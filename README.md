# delta-syncer

A **file synchronisation utility** that monitors a given directory and syncs changes to a remote server using **delta sync**.
Built for **bandwidth-constrained environments** - only transferring** changed chunks of files**, rather than entire files.

## Project Structure

### `common/chunker.py`
The core chunking module used by both client and server.
- Breaks files into chunks
- Computes SHA-256 Hash of each chunk and the whole file
- Auto-scales chunk size: starts at 128 KB, doubles when chunk count exceeds 2000, capped at 16 MB
- `chunk_file()` - called by the client before syncing. It returns a manifest: whole-file SHA-256, chunk size, and a list of chunk records (index, hash). The client sends this manifest to the server so it can determine which chunks it needs (avoiding unnecessary sends over the wire)
  - whole-file SHA-256 - After reassembly, the server computes the SHA-256 of the complete file and compares it against this hash to verify the reassembled file is an exact copy of the client's
  - `chunk_size` - the server needs to slice unchanged chunks from the existing backup during reassembly (so it can insert the changed chunks in between). Without it, the server would not know the boundaries of the chunks in the old file
    - `index` identifies WHICH NUMBER chunk of the file it is (e.g. 0, 1, 2, ...). The server needs this to know which chunk is being compared
    - `sha256` the HASH of THAT chunk's bytes
      - used by the server to check if the chunk in question differs from its respective one on the server (to determine if the chunk needs to be sent)
      - also, if there is change, and the chunk is sent to the server, the sent chunk's hash is computed (and compared to see if it matches the hash before it was sent - to check if there was any corruption during transit)
- `yield_records()` - reads the file, in chunks, and yields both the record (index of chunk, hash of chunk) and the raw bytes (the chunk itself), for each chunk
  - used in `chunk_file` to help build the manifest (i.e. use the raw bytes to compute the full file hash, get the record) 
- `compare_chunk()` - called by the server when a chunk arrives. Recomputes the SHA-256 of the received raw bytes and compares against the hash that the client sent to ensure it matches (and did not corrupt during transfer)

### `common/manifest.py`
Finds the indices of the chunks needed by the server from the client, to update/sync.
Uses the client and server chunk records and compares the hashes of each chunk (by index). Returns the indices of the chunks that did not match and require the updated chunk to be sent.

### `client/state_db.py`
Client-side SQLite database for tracking file state
- Stores mtime, size, and SHA-256 hash for each watched file
- Two stage change detection
    1. Cheap meta-data check (mtime and size) - if no changes, assumed the same
    2. Expensive hash check if stage 1 detects change (recomputes hash and checks if it changed from what is stored)
- Contains the DB operations for additions, modifications, deletions

### `client/client.py`
The main client application
- Uses `watchdog` to monitor the `client_dir/` directory for filesystem events
- Event handlers update the state database and trigger sync for changed files
- `reconcile()` runs on startup and periodically (every 30s) to catch changes missed while offline or during heavy I/O (event queue overflow)
- `sync_file()` orchestrates the full sync pipeline: chunk the file, get needed chunk indices from server, send only those chunks, reassemble on server, verify


### `server/server.py`
Server side logic for receiving and reassembling files
- SQLite database tracks synced files (path, whole-file SHA-256, chunk size, chunk count) and individual chunk records (path, index, hash)
- `get_needed_chunks()` compares the sent client chunk records against stored records, returns the indices of the chunks the server needs
- `reassemble_file()` rebuilds the full file from staged (changed) chunks and existing backup (unchanged chunks), writes to a `.tmp` file, verifies the whole-file hash, then atomically replaces the old backup
- `finalise_sync()` updates the server DB and cleans up staging files

### `tests/test_client.py`
Unit tests for the client module (state_db + client event handlers).
- Tests change detection, event handling (create, modify, delete, move), and reconciliation
- Run with: `python -m pytest tests/test_client.py -v`

### NOTE
- No transport layer included - in production, chunks would be sent encrypted over TLS 1.3/TCP
- For the case where a file grows or shrink enough to trigger a different chunk size, the code currently handles this by;
    - when the server tries to match hashes of chunks, they no longer do, so all chunks are resent
    - then, during reassembly, since chunk size changed, all chunks are new, and the old chunk size isnt used
- For filesystems where event-driven monitoring is unavailable (e.g. Windows drives mounted in WSL via /mnt/c/), changes are noticed through polling only
  
## DEMO
Open a terminal and start the client `python -m client.client`

In another terminal -  copy or create files in `client/client_dir/`:
```
# Create a test file
echo "hello" > client/client_dir/test.txt

# Create a larger binary file
dd if=/dev/urandom of=client/client_dir/big.bin bs=1M count=5
```
The client detects the change, chunks the file, sends only the needed chunks to the server, and the server reassembles the file in `server/backup_dir/`.

### Verifying integrity
```bash
sha256sum client/client_dir/big.bin server/backup_dir/big.bin
```
Both hashes should match - confirming the file was transferred and reassembled correctly.

