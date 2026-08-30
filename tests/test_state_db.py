import os
import sqlite3
import pytest

from client.state_db import (
    init_db,
    compute_file_sha256,
    get_file_state,
    read_consistent_state,
    update_file_state,
    remove_file_state,
    remove_dir_state,
    rename_file_state,
    rename_dir_state,
    is_sync_needed,
)

# Note: I generated these tests using Claude
# use via python -m pytest tests/test_state_db.py -v

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory SQLite connection with the file_info_tbl created."""
    c = sqlite3.connect(":memory:")
    init_db(c)
    return c


@pytest.fixture
def tmp_file(tmp_path):
    """Create a temporary file with known content and return its path."""
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    return str(f)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_table(conn):
    """Table should exist after init_db."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_info_tbl'")
    assert cursor.fetchone() is not None


def test_init_db_twice_no_error(conn):
    """Calling init_db a second time should not raise."""
    init_db(conn)  # already called in fixture — second call should be fine


# ---------------------------------------------------------------------------
# compute_file_sha256
# ---------------------------------------------------------------------------

def test_compute_sha256_known_content(tmp_file):
    """Hash should match the known SHA-256 of 'hello world'."""
    import hashlib
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert compute_file_sha256(tmp_file) == expected


def test_compute_sha256_empty_file(tmp_path):
    """Empty file should produce the SHA-256 of zero bytes."""
    import hashlib
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    expected = hashlib.sha256(b"").hexdigest()
    assert compute_file_sha256(str(f)) == expected


# ---------------------------------------------------------------------------
# get_file_state / update_file_state
# ---------------------------------------------------------------------------

def test_get_file_state_unknown(conn):
    """Untracked file returns None."""
    assert get_file_state(conn, "/no/such/file.txt") is None


def test_update_then_get(conn, tmp_file):
    """After update_file_state, get_file_state should return the stored record."""
    update_file_state(conn, tmp_file)
    state = get_file_state(conn, tmp_file)
    assert state is not None
    assert state["size"] == os.path.getsize(tmp_file)
    assert state["sha256"] == compute_file_sha256(tmp_file)


# ---------------------------------------------------------------------------
# read_consistent_state
# ---------------------------------------------------------------------------

def test_read_consistent_state_stable_file(tmp_file):
    """A file that isn't being modified should return a valid tuple."""
    result = read_consistent_state(tmp_file)
    assert result is not None
    mtime, size, sha256 = result
    assert size == os.path.getsize(tmp_file)


# ---------------------------------------------------------------------------
# remove_file_state
# ---------------------------------------------------------------------------

def test_remove_file_state(conn, tmp_file):
    """After removing, get_file_state should return None."""
    update_file_state(conn, tmp_file)
    assert get_file_state(conn, tmp_file) is not None

    remove_file_state(conn, tmp_file)
    assert get_file_state(conn, tmp_file) is None


# ---------------------------------------------------------------------------
# remove_dir_state
# ---------------------------------------------------------------------------

def test_remove_dir_state_clears_descendants(conn, tmp_path):
    """All files under the directory should be removed from the DB."""
    # Create and track two files under /docs
    docs = tmp_path / "docs"
    docs.mkdir()
    f1 = docs / "a.txt"
    f2 = docs / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    update_file_state(conn, str(f1))
    update_file_state(conn, str(f2))

    remove_dir_state(conn, str(docs))

    assert get_file_state(conn, str(f1)) is None
    assert get_file_state(conn, str(f2)) is None


def test_remove_dir_state_no_false_match(conn, tmp_path):
    """Deleting '/docs' must not remove '/docs2/file.txt'."""
    docs = tmp_path / "docs"
    docs2 = tmp_path / "docs2"
    docs.mkdir()
    docs2.mkdir()

    f1 = docs / "a.txt"
    f2 = docs2 / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    update_file_state(conn, str(f1))
    update_file_state(conn, str(f2))

    remove_dir_state(conn, str(docs))

    assert get_file_state(conn, str(f1)) is None
    assert get_file_state(conn, str(f2)) is not None  # should survive


# ---------------------------------------------------------------------------
# rename_file_state
# ---------------------------------------------------------------------------

def test_rename_file_state(conn, tmp_file):
    """Record should move from old path to new path."""
    update_file_state(conn, tmp_file)
    new_path = tmp_file + ".renamed"

    rename_file_state(conn, tmp_file, new_path)

    assert get_file_state(conn, tmp_file) is None
    assert get_file_state(conn, new_path) is not None


def test_rename_file_state_overwrites_dest(conn, tmp_path):
    """Renaming onto an existing tracked path should replace the old record."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    update_file_state(conn, str(f1))
    update_file_state(conn, str(f2))

    # Rename a.txt -> b.txt (overwrites b.txt's record)
    rename_file_state(conn, str(f1), str(f2))

    assert get_file_state(conn, str(f1)) is None
    state = get_file_state(conn, str(f2))
    assert state is not None
    # The hash should be a.txt's hash (the source), not b.txt's
    assert state["sha256"] == compute_file_sha256(str(f1))


# ---------------------------------------------------------------------------
# rename_dir_state
# ---------------------------------------------------------------------------

def test_rename_dir_state(conn, tmp_path):
    """All descendant paths should be rewritten from src to dest prefix."""
    src = tmp_path / "old"
    src.mkdir()
    f1 = src / "a.txt"
    f2 = src / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    update_file_state(conn, str(f1))
    update_file_state(conn, str(f2))

    dest = tmp_path / "new"
    rename_dir_state(conn, str(src), str(dest))

    # Old paths gone
    assert get_file_state(conn, str(f1)) is None
    assert get_file_state(conn, str(f2)) is None
    # New paths present
    assert get_file_state(conn, str(dest / "a.txt")) is not None
    assert get_file_state(conn, str(dest / "b.txt")) is not None


# ---------------------------------------------------------------------------
# is_sync_needed
# ---------------------------------------------------------------------------

def test_is_sync_needed_new_file(conn, tmp_file):
    """A file not in the DB needs syncing."""
    assert is_sync_needed(conn, tmp_file) is True


def test_is_sync_needed_unchanged(conn, tmp_file):
    """After updating, an untouched file should not need syncing."""
    update_file_state(conn, tmp_file)
    assert is_sync_needed(conn, tmp_file) is False


def test_is_sync_needed_content_changed(conn, tmp_path):
    """A file whose content changed should need syncing."""
    import time
    f = tmp_path / "data.txt"
    f.write_text("original")
    update_file_state(conn, str(f))

    time.sleep(0.05)
    f.write_text("modified")
    assert is_sync_needed(conn, str(f)) is True

def test_is_sync_needed_deleted_file(conn, tmp_path):
    """A file that was deleted should return False (deletion handled elsewhere)."""
    f = tmp_path / "gone.txt"
    f.write_text("temp")
    update_file_state(conn, str(f))

    os.remove(str(f))
    assert is_sync_needed(conn, str(f)) is False