import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import sqlite3

from state_db import (
    DB_PATH,
    init_db,
    is_sync_needed,
    update_file_state,
    remove_file_state,
    remove_dir_state,
    rename_file_state,
    rename_dir_state,
)
# ----------
# Fallback: polling-based observer for filesystems where event-driven monitoring is unavailable
# (e.g. Windows drives mounted in WSL via /mnt/c/)

# from watchdog.observers.polling import PollingObserver
# ----------

# The Path to the Client Directory we want to monitor
CLIENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "client_dir")) # __file__ resolves to the actual location of this script on disk

POLL_INTERVAL = 30 # seconds between periodic scans


# ------------------------ HELPERS --------------------

def sync_file(filepath):
    """Placeholder for the 'transport layer'"""

    print(f"[SYNCING] {filepath} - placeholder")


def handle_file_change(conn, filepath):
    """Check if a file needs syncing, and if so, sync it and record the new state"""
    if is_sync_needed(conn, filepath):
        sync_file(filepath)
        result = update_file_state(conn, filepath)
        if result is None:
            print(f"[UNSTABLE] {filepath} - file changed during hashing, will retry later")


# ---------------- Event Handlers ---------------------


class SyncHandler(FileSystemEventHandler): # Inherits the FileSystemEventHandler class from watchdog.events
    """Handles filesystem events for synchronisation purposes"""

    # Overriding the provided methods of FileSystemEventHandler to handle specific events
    # These methods will be called when the respective events occur 
    # The observer is designed to monitor for changes and trigger the appropriate event handlers.
    # https://pythonhosted.org/watchdog/api.html

    def __init__(self, conn, watch_root):
        super().__init__() # run the parent class (FileSystemEventHandler setup first)
        # self is the instance being created
        # conn and watch_root are args passed in by caller (i.e. DB connection and dir path)
        self.conn = conn
        self.watch_root = os.path.abspath(watch_root)

    def in_watch_root(self, path):
        """True if path is inside (or equal to) the watched directory."""
        try:
            # Find the longest shared path between the passed in path and the watch path
            # If it returns the watch path itself, the path is getting watched
            return os.path.commonpath(
                [os.path.abspath(path), self.watch_root]
            ) == self.watch_root
        except ValueError:
            return False

        
    # ---------- FILE EVENTS ---------------------
    
    def on_created(self, event):
        if event.is_directory:
            # print(f"[DIR CREATED] {event.src_path}")
            # watchdog fires seperate on_created events for each file inside the dir
            return
        try:
            print(f"[FILE CREATED] {event.src_path}")
            handle_file_change(self.conn, event.src_path)
        except Exception as e:
            print(f"[ERROR] on_created: {event.src_path} - {e}")


    def on_deleted(self, event):
        try:
            if event.is_directory:
                remove_dir_state(self.conn, event.src_path)
                print(f"[DIR DELETED] {event.src_path} (descendants cleared)")
                return
            remove_file_state(self.conn, event.src_path)
            print(f"[FILE DELETED] {event.src_path}")
        except Exception as e:
            print(f"[ERROR] on_deleted: {event.src_path} - {e}")

    def on_modified(self, event):
        if event.is_directory:
            # print(f"[DIR MODIFIED] {event.src_path}")
            # watchdog fires seperate on_modified events for each file inside the dir
            return
        try:
            print(f"[FILE MODIFIED] {event.src_path}")
            handle_file_change(self.conn, event.src_path)
        except Exception as e:
            print(f"[ERROR] on_modified: {event.src_path} - {e}")

    def on_moved(self, event):
        """Handle renames and moves
        
        Moving files OUT of the client_dir (the dir being watched) fires on_moved
        with dest_path outside watch root.
        """

        try:
            # Checking if the destination the file was moved to is still inside the dir being watched
            dest_inside = self.in_watch_root(event.dest_path)

            # Dir move
            if event.is_directory:
                if dest_inside:
                    rename_dir_state(self.conn, event.src_path, event.dest_path)
                    print(f"[DIR MOVED] {event.src_path} → {event.dest_path}")
                else:
                    # moved out of directory getting watched - treat every descendant as deleted
                    remove_dir_state(self.conn, event.src_path)
                    print(f"[DIR MOVED OUT] {event.src_path} (descendants removed)")
                return

            # File move
            if dest_inside:
                rename_file_state(self.conn, event.src_path, event.dest_path)
                print(f"[FILE MOVED] {event.src_path} → {event.dest_path}")
            else:
                # moved out of directory getting watched - equivalent of deleted 
                remove_file_state(self.conn, event.src_path)
                print(f"[MOVED OUT] {event.src_path} (treated as delete)")

        except Exception as e:
           print(f"[ERROR] on_moved: {event.src_path} → {getattr(event, 'dest_path', '?')} - {e}") 


# --------RECONCILIATION--------------------------------

def reconcile(conn, watch_root):
    """Walk the watched dir and update respectively to consider changes while the client was offline"""

    print("[RECONCILE] Starting scan...")


    # Check every file on disk against the database
    for dirpath, _, filenames in os.walk(watch_root):
        for fname in filenames:
            filepath = os.path.join(dirpath, fname)
            try:
                handle_file_change(conn, filepath)
            except Exception as e:
                print(f"[RECONCILE ERROR] {filepath} - {e}")

    # Delete database rows of files that no longer exist on disk
    cursor = conn.execute("SELECT path FROM file_info_tbl")

    # list of paths in DB that have been removed from watched dir (i.e. if the path no longer exists in the watched dir on disk)
    stale_paths =  [row[0] for row in cursor.fetchall() if not os.path.exists(row[0])]
    for path in stale_paths:
        remove_file_state(conn, path)
        print(f"[RECONCILE] Removed stale record: {path}")
    print("[RECONCILE] Scan complete.")


# ------------MAIN----------------------------------
def main():
    # Create a dir to watch (CLIENT_DIR) if it does not already exist
    if not os.path.exists(CLIENT_DIR):
        os.makedirs(CLIENT_DIR)
        print(f"Created a directory to watch/sync: {CLIENT_DIR}")

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)


    # Run reconciliation to catch any changes while client was offline
    reconcile(conn, CLIENT_DIR)

    # FILE SYSTEM WATCHER


    observer = Observer() # Creating an observer object that will monitor the filesystem for changes
    # observer = PollingObserver(timeout=2) # fall back - polling
    
    observer.schedule(SyncHandler(conn, CLIENT_DIR), CLIENT_DIR, recursive=True) # Scheduling the observer to monitor the CLIENT_DIR (recursively to consider subdirs) and handle events with SyncHandler
    observer.start() # Starts the observer (in a separate thread, to not block main script execution)
    print(f"WATCHING: {CLIENT_DIR} for changes...")

    try:
        last_poll = time.time()
        while True:
            time.sleep(1) # Keep the main thread alive to allow the observer to run

            # Periodic Polling
            if time.time() - last_poll >= POLL_INTERVAL:
                reconcile(conn, CLIENT_DIR)
                last_poll = time.time()
    except KeyboardInterrupt:
        print("STOPPED WATCHING, exiting program...")
        observer.stop() # Stop the observer if the user interrupts the program (e.g., Ctrl+C)

    observer.join() # Wait for the observer thread to finish before exiting the program
    conn.close()

if __name__ == "__main__":
    main() # Run main function when script executed directly