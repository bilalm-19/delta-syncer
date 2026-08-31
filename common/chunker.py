import os
import hashlib

MIN_CHUNK_SIZE = 128 * 1024 # 128KB
MAX_CHUNK_SIZE = 16 * 1024 * 1024 # 16 MB

# The max number of chunks, before stopping increasing chunk size
MAX_CHUNKS_PER_FILE = 2000 # chunks

def hash_bytes(data):
    """SHA-256 of a block of bytes, as hex (for the manifest)"""
    return hashlib.sha256(data).hexdigest()


def compare_chunk(recv_chunk, stored_chunk_hash):
    """Compare the recieved (on the server) chunk's hash to the stored chunk hash"""
    return hash_bytes(recv_chunk) == stored_chunk_hash

def decide_chunk_sz(file_size, min_sz=MIN_CHUNK_SIZE, max_sz=MAX_CHUNK_SIZE):
    """Pick the smallest chunk size the file can use without its manifest becoming too large
    
    Small Chunks - preferable - finer change detection and less wasted on resend
    Tradeoff - the many chunk records would cause large manifest sizes (more hashes to be stored)
    
    Thus, trade off managed by - min_sz is the default used, MAX_CHUNKS_PER_FILE is what overrides
    it, increasing chunk size until it produces fewer than MAX_CHUNKS_PER_FILE chunks    
    or the max_sz is reached
    """

    size = min_sz # what we want - only grows if forced by number of chunks it produces

    # Each doubling halves the chunk count - Stop at the first size that fits
    # or at the ceiling (max_sz)
    while size < max_sz and file_size / size > MAX_CHUNKS_PER_FILE:
        size *= 2

    # Choose the smaller chunk size of the two
    # Considers the case if the selected size exceeds the max size for a chunk
    return min(size, max_sz)


def yield_records(fileobj, chunk_sz):
    """Read fileobj in chunk_sz pieces, yielding (record, raw_bytes) for each
    
    Each record is a plain dict:  {"index": int, "offset": int, "length": int, "sha256": str}

    Final chunk may be shorter than chunk_sz. Length is stored so the server can read the
    correct number of bytes
    
    """

    index = 0 # which chunk it is
    offset = 0 # where in the file that chunk starts

    while True:
        data = fileobj.read(chunk_sz)
        if not data:
            break

        record ={
            "index": index,
            "offset": offset,
            "length": len(data),
            "sha256": hash_bytes(data),
        }
        yield record, data

        offset += len(data)
        index += 1

def chunk_file(filepath, chunk_sz=None):
    """Chunk a file and return the chunks / records for the client"""

    file_size = os.path.getsize(filepath)
 
    if chunk_sz is None:
        chunk_sz = decide_chunk_sz(file_size)

    records = []
    file_hasher = hashlib.sha256()
 
    with open(filepath, "rb") as fh:
        for record, data in yield_records(fh, chunk_sz):
            records.append(record)
            file_hasher.update(data)
 
    return {
        "size":       file_size,
        "sha256":     file_hasher.hexdigest(), # hash of the entire file
        "chunk_size": chunk_sz,
        "records":     records,
    }
 




    



