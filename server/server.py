import os
import sqlite3
import hashlib


from common.chunker import compare_chunk, hash_bytes
from common.manifest import compare_records

SERVER_DIR = os.path.abspath(os.path.dirname(__file__))
BACKUP_DIR = os.path.join(SERVER_DIR, "backup_dir")
DB_PATH = os.path.join(SERVER_DIR, "server_state.db")
STAGING_DIR = os.path.join(SERVER_DIR, "staging")

def init_db(conn):
    """Create the server's tables if they do not exist
    
    file_info_tbl - info (path, file hash, chunk size, num chunks) of the files on the server
    
    chunk_records_tbl - (path, chunk index) and the respective chunk hash
    """

    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_info_tbl (
            path TEXT PRIMARY KEY,
            file_sha256 TEXT,
            chunk_size INTEGER,
            num_chunks INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunk_records_tbl (
            path TEXT,
            idx INTEGER,
            sha256 TEXT,
            PRIMARY KEY (path, idx)
        )
    """)

    conn.commit()


def get_needed_chunks(conn, filepath, client_records):
    """Compare the passed in client's chunk records against the stored record and return the indices of the chunks needed"""

    cursor = conn.execute(
        "SELECT idx, sha256 FROM chunk_records_tbl where pth = ? ORDER BY idx",
        (filepath,)
    )

    server_records = [{"index": row[0], "sha256": row[1]} for row in cursor.fetchall()]

    return compare_records(client_records, server_records)


def receive_chunk(filepath, chunk_index, data, client_chunk_sha256):
    if not compare_chunk(data, client_chunk_sha256):
        return False

    file_staging_dir = os.path.join(STAGING_DIR, filepath)
    os.makedirs(file_staging_dir, exist_ok=True)
    chunk_path = os.path.join(file_staging_dir, str(chunk_index))

    # write the chunk to the staging path
    with open(chunk_path, "wb") as f:
        f.write(data)

    return True

def reassemble_file(conn, filepath, num_chunks, client_file_sha256):
    """Reassemble the staged chunks into the final file
    Returns True if the whole-file hash now on the server matches the
    whole-file hash of the file on the client side wanting to be synced"""

    # The dir that holds the changed/new chunks
    file_staging_dir = os.path.join(STAGING_DIR, filepath)

    # The path where the backup copy (i.e. the last saved sync) is
    output_path = os.path.join(BACKUP_DIR, filepath)

    file_hasher = hashlib.sha256()

    with open(output_path, "wb") as out:
        for i in range(num_chunks):
            # e.g. file_staging_dir = "/tmp/staging" and i = 3
            # gives us /tmp/staging/3
            chunk_path = os.path.join(file_staging_dir, str(i))

            with open(chunk_path, "rb") as chunk_file:
                data = chunk_file.read()
                out.write(data)
                file_hasher.update(data) # to feed the data to get hashed, piece by piece (avoid loading entire file at once)

    if file_hasher.hexdigest() != client_file_sha256:
        # reassembled hash mismatches client's file hash
        # i.e. reassembled file is corrupt
        os.remove(output_path)
        return False

    return True

def finalise_sync(conn, filepath, client_records, file_sha256, chunk_size):
    """Update the server records and clean up staging after successful reassembly"""

    # Update file-level info
    conn.execute(
        "INSERT OR REPLACE INTO file_info_tbl (path, file_sha256, chunk_size, num_chunks) VALUES (?, ?, ?, ?)",
        (filepath, file_sha256, chunk_size, len(client_records))
    )

    # Clear old chunk records for this file (handles shrunk files too)
    conn.execute("DELETE FROM chunk_records_tbl WHERE path = ?", (filepath,))

    # Insert the current records
    for record in client_records:
        conn.execute(
            "INSERT INTO chunk_records_tbl (path, idx, sha256) VALUES (?, ?, ?)",
            (filepath, record["index"], record["sha256"])
        )

    conn.commit()

    # Clean up staging
    file_staging_dir = os.path.join(STAGING_DIR, filepath)
    if os.path.exists(file_staging_dir):
        for f in os.listdir(file_staging_dir):
            os.remove(os.path.join(file_staging_dir, f))
        os.rmdir(file_staging_dir)   
