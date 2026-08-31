def compare_records(client_records, server_records):
    """Compare the hashes of each chunk (by index),
    return the indices (of the chunks) that did not match and, thus,
    require the updated chunk to be sent"""

    chunk_needed_idxs = []

    for idx, client_rec in enumerate(client_records):
        if idx >= len(server_records):
            chunk_needed_idxs.append(idx)
        elif client_rec["sha256"] != server_records[idx]["sha256"]:
            chunk_needed_idxs.append(idx)

    return chunk_needed_idxs