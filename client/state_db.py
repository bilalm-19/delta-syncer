import os
import sqlite3
import hashlib
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "client_state.db")

# Buffer size for reading files during hashing (to avoid loading entire file into memory at once)
READ_BUFFER_SZ = 128 * 1024 # 128 KB

# Max number of attempt and retry delay for when considering for inconsistent reads (e.g. file modifications during hashing)
MAX_READ_ATTEMPTS = 3
RETRY_DELAY = 0.15 # seconds between attempts, to let the writer finish
 

def init_db(conn):
    """Ensure the files table exists on the given connection."""

    # path --> text, primary key (uniquely identifies the row - no duplicates, no nulls)
    # mtime --> REAL (decimal number)
    # size --> INTEGER
    # sha256 --> TEXT
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_info_tbl (
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER,
            sha256 TEXT
        )
    """) 

    conn.commit() # save database changes
    return


def compute_file_sha256(filepath):
    """Compute the SHA-256 hash of an entire file
    File is read in fixed-size chunks, rather than all at once,
    avoiding loading the entire file at once, to consider memory usage
    """

    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f: # rb as reading binary - need the bytes to compute the hash
        while True:
            buffer = f.read(READ_BUFFER_SZ)
            if not buffer:
                break
            sha256_hash.update(buffer)

    return sha256_hash.hexdigest() # hexdigest as goes into SQLite sha256 TEXT field, which prints readably in logs

def get_file_state(conn, filepath):
    """Retrieve the stored state of a file from the DB
    Returns None if no record exists"""

    # cursor is a pointer to a result set of a query
    # so we can execute queries, fetch results, and iterate over datasets
    cursor = conn.execute("SELECT mtime, size, sha256 from file_info_tbl WHERE path = ?", (filepath,))
    row = cursor.fetchone() # returns the next row
    if row:
        return {"mtime": row[0], "size": row[1], "sha256": row[2]}
    return None

def read_consistent_state(filepath):
    """Read a file's meatadata and hash, enusring file was not modified during hashing"""
    # Hashing not instantaneous, so file may be modified mid-hash calculation
    # stat_before and stat_after are used to confirm the metadata and hash describe the same file state
    #   if they differ, there was change during hashing (race condition)
    stat_before = os.stat(filepath) # a snapshot - get the file's metadata (size, mtime, etc.)
    sha256 = compute_file_sha256(filepath)
    stat_after = os.stat(filepath)

    # On mismatch, return None
    if (stat_before.st_mtime != stat_after.st_mtime or stat_before.st_size != stat_after.st_size):
        return None

    return stat_after.st_mtime, stat_after.st_size, sha256



def update_file_state(conn, filepath, max_attempts=MAX_READ_ATTEMPTS):
    """Update the database with the current state of a file (mtime, size, sha256)"""

    for attempt in range(max_attempts):
        state = read_consistent_state(filepath)

        if state is not None:
            mtime, size, sha256 = state
            conn.execute("INSERT OR REPLACE INTO file_info_tbl (path, mtime, size, sha256) VALUES (?, ?, ?, ?)",
                         (filepath, mtime, size, sha256))
            conn.commit()
            return {"mtime": mtime, "size": size, "sha256": sha256}

        if attempt < max_attempts - 1: # as attempt in range of 0,1,2 (index from 0)
            time.sleep(RETRY_DELAY)

    # still unstable after every attempt - leave untracked so a later event retries
    return None 

def remove_file_state(conn, filepath):
    """Remove a file's record from the DB (e.g. when file is deleted)"""
    conn.execute("DELETE from file_info_tbl WHERE path = ?", (filepath,))
    conn.commit()
    return

def remove_dir_state(conn, dirpath):
    """Remove all files beneath a deleted dir"""
    conn.execute("DELETE FROM file_info_tbl WHERE path LIKE ?", (dirpath + os.sep + '%',))
    conn.commit()
    return

def rename_file_state(conn, src_path, dest_path):
    """Repoint an existing record at a new path after a file is renamed/moved
    Rename does not alter file contents (stored mtime, size, hash, remain valid)
    """

    # A rename can overwrite an existing tracked file
    # e.g. mv a.txt b.txt - path is the primary key, so old dst record
    # must be cleared first

    with conn:
        # Delete the path that got overwritten (i.e. where our new destination is)
        # if no file is getting overwritten, does nothing
        conn.execute("DELETE FROM file_info_tbl WHERE path = ?", (dest_path,))
        # Update so that the path of the file (source) to overwrite the destination
        # now points to the destination
        conn.execute("UPDATE file_info_tbl SET path = ? WHERE path = ?", (dest_path, src_path))
    return


def rename_dir_state(conn, src_dir, dest_dir):
    """Repoint every record beneath a renamed/moved directory
    watchdog gives a single move event for the directory itself, not per file inside it,
    so every descendant path must be rewritten. Contents are unchanged
 
    substr() is 1-indexed in SQLite, so len(src_dir) + 1 is the character after
    the old prefix - the remainder of each path is appended to the new prefix.
    """

    # || is concat in SQL
    # E.G. from /docs to /archive :  '/archive' || substr('/docs/a.txt', 6) = '/archive/a.txt'
    # trailing separator in the LIKE pattern stops, for e.g., '/docs' also matching '/docs2'
    
    # A directory move cannot overwrite live files (os.rename refuses a non-empty
    # destination), but stale records can linger under it if a deletion event was
    # missed. path is the primary key, so those must be cleared or the UPDATE fails.
    with conn:
        # Clear stale records under the destination (DB rows only, no files touched)
        conn.execute("DELETE FROM file_info_tbl WHERE path LIKE ?", (dest_dir + os.sep + '%',))
        # Repoint every record under the source at the destination (contents unchanged)
        conn.execute("""
            UPDATE file_info_tbl SET path = ? || substr(path, ?) WHERE path LIKE ?""",
                (dest_dir, len(src_dir) + 1, src_dir + os.sep + '%'))
    return

def is_sync_needed(conn, filepath):
    """Determine whether a file needs syncing, comparing it against the stored state
            Two Stages - Cheapest Check runs first (to avoid unnecessary expensive hash checks)

                1. Cheap Check - compare mtime and size (metadata only, no file read)
                    If both match, file is assumed unchanged, no hash computed

                2. Expensive (Hash) Check - only if quick check flagged a difference
                    Check if the content actually changed
                    If no content change (just the metadata in stage 1) then avoid the pointless transfer of data over the network
    
                    Note: mtime and size are client-side change detection signals only - they are
                    never sent to the server. Only changed content is transferred.
    """

    # File no longer exists - deleted between the event firing and this check
    # Deletion handled separately (remove_file_state()) - nothing to sync here
    if not os.path.exists(filepath):
        return False
 
    stored = get_file_state(conn, filepath)
 
    # Not tracked yet - a new file needs 'updating'
    if stored is None:
        return True
 
    stat = os.stat(filepath)
 
    # Stage 1 - metadata unchanged, so assume the content is unchanged too
    if stat.st_mtime == stored["mtime"] and stat.st_size == stored["size"]:
        return False
 
    # Stage 2 - metadata moved, so confirm against the content hash
    current_sha256 = compute_file_sha256(filepath)
 
    if current_sha256 == stored["sha256"]:
        # Means the content is identical - only mtime moved (e.g. a resave of the same bytes)
        # Refresh the stored metadata locally so the cheap check passes next time
        # to avoid future unnecessary hashing
        # Local DB write only, nothing is sent over the network here
        conn.execute("UPDATE file_info_tbl SET mtime = ?, size = ? WHERE path = ?",
                     (stat.st_mtime, stat.st_size, filepath))
        conn.commit()
        return False
 
    return True
