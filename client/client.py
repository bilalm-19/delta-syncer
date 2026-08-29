import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ----------
# Fallback: polling-based observer for filesystems where event-driven monitoring is unavailable
# (e.g. Windows drives mounted in WSL via /mnt/c/)

# from watchdog.observers.polling import PollingObserver
# ----------

# The Path to the Client Directory we want to monitor
CLIENT_DIR = os.path.join(os.path.dirname(__file__), "client_dir") # __file__ resolves to the actual location of this script on disk

class SyncHandler(FileSystemEventHandler): # Inherits the FileSystemEventHandler class from watchdog.events
    """Handles filesystem events for synchronisation purposes"""

    # Overriding the provided methods of FileSystemEventHandler to handle specific events
    # These methods will be called when the respective events occur 
    # The observer is designed to monitor for changes and trigger the appropriate event handlers.
    # https://pythonhosted.org/watchdog/api.html
    
    def on_created(self, event):
        if event.is_directory:
            print(f"[DIR CREATED] {event.src_path}")
            return
        print(f"[FILE CREATED] {event.src_path}")

    def on_deleted(self, event):
        if event.is_directory:
            print(f"[DIR DELETED] {event.src_path}")
            return
        print(f"[FILE DELETED] {event.src_path}")

    def on_modified(self, event):
        if event.is_directory:
            print(f"[DIR MODIFIED] {event.src_path}")
            return
        print(f"[FILE MODIFIED] {event.src_path}")

    def on_moved(self, event):
        # e.g. due to renaming
        if event.is_directory:
            print(f"[DIR MOVED] {event.src_path} → {event.dest_path}")
            return
        print(f"[FILE MOVED] {event.src_path} → {event.dest_path}")


def main():
    # Create a dir to watch (CLIENT_DIR) if it does not already exist
    if not os.path.exists(CLIENT_DIR):
        os.makedirs(CLIENT_DIR)
        print(f"Created a directory to watch/sync: {CLIENT_DIR}")

    observer = Observer() # Creating an observer object that will monitor the filesystem for changes

    # observer = PollingObserver(timeout=2) # fall back polling
    
    observer.schedule(SyncHandler(), CLIENT_DIR, recursive=True) # Scheduling the observer to monitor the CLIENT_DIR (recursively to consider subdirs) and handle events with SyncHandler
    observer.start() # Starts the observer (in a separate thread, to not block main script execution)
    print(f"WATCHING: {CLIENT_DIR} for changes...")

    try:
        while True:
            time.sleep(1) # Keep the main thread alive to allow the observer to run
    except KeyboardInterrupt:
        print("STOPPED WATCHING, exiting program...")
        observer.stop() # Stop the observer if the user interrupts the program (e.g., Ctrl+C)
        observer.join() # Wait for the observer thread to finish before exiting the program


if __name__ == "__main__":
    main() # Run main function when script executed directly