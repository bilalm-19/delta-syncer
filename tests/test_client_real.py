import os
import sqlite3
import pytest
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    DirDeletedEvent,
    DirMovedEvent,
)
from client.state_db import init_db, get_file_state, update_file_state
from client.client import SyncHandler, handle_file_change, reconcile

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
def watch_dir(tmp_path):
    """A temporary directory acting as the watched client_dir."""
    d = tmp_path / "client_dir"
    d.mkdir()
    return d


@pytest.fixture
def handler(conn, watch_dir):
    """A SyncHandler wired to the in-memory DB and temp watch dir."""
    return SyncHandler(conn, str(watch_dir))


# ---------------------------------------------------------------------------
# in_watch_root
# ---------------------------------------------------------------------------

def test_in_watch_root_inside(handler, watch_dir):
    """A path inside the watch dir should return True."""
    assert handler.in_watch_root(str(watch_dir / "file.txt")) is True


def test_in_watch_root_outside(handler, tmp_path):
    """A path outside the watch dir should return False."""
    outside = tmp_path / "other" / "file.txt"
    assert handler.in_watch_root(str(outside)) is False


def test_in_watch_root_itself(handler, watch_dir):
    """The watch dir itself counts as inside."""
    assert handler.in_watch_root(str(watch_dir)) is True


# ---------------------------------------------------------------------------
# handle_file_change
# ---------------------------------------------------------------------------

def test_handle_file_change_new_file(conn, watch_dir):
    """A new file should be synced and tracked in the DB."""
    f = watch_dir / "new.txt"
    f.write_text("content")

    handle_file_change(conn, str(f))

    assert get_file_state(conn, str(f)) is not None


def test_handle_file_change_unchanged(conn, watch_dir):
    """An unchanged file should not update the DB again."""
    f = watch_dir / "same.txt"
    f.write_text("content")

    handle_file_change(conn, str(f))
    state_before = get_file_state(conn, str(f))

    handle_file_change(conn, str(f))
    state_after = get_file_state(conn, str(f))

    assert state_before == state_after


# ---------------------------------------------------------------------------
# SyncHandler — on_created
# ---------------------------------------------------------------------------

def test_on_created_file(handler, conn, watch_dir):
    """on_created for a file should track it in the DB."""
    f = watch_dir / "created.txt"
    f.write_text("hello")

    event = FileCreatedEvent(str(f))
    handler.on_created(event)

    assert get_file_state(conn, str(f)) is not None


def test_on_created_directory_ignored(handler, conn, watch_dir):
    """on_created for a directory should do nothing."""
    d = watch_dir / "subdir"
    d.mkdir()

    event = FileCreatedEvent(str(d))
    event._is_directory = True  # make it look like a dir event
    # Use a proper DirCreatedEvent instead
    from watchdog.events import DirCreatedEvent
    dir_event = DirCreatedEvent(str(d))
    handler.on_created(dir_event)

    # No file record should exist for the directory
    assert get_file_state(conn, str(d)) is None


# ---------------------------------------------------------------------------
# SyncHandler — on_modified
# ---------------------------------------------------------------------------

def test_on_modified_file(handler, conn, watch_dir):
    """on_modified should update the DB when content changes."""
    f = watch_dir / "mod.txt"
    f.write_text("original")

    # Track it first
    event = FileCreatedEvent(str(f))
    handler.on_created(event)
    state_before = get_file_state(conn, str(f))

    # Modify it
    f.write_text("changed")
    event = FileModifiedEvent(str(f))
    handler.on_modified(event)
    state_after = get_file_state(conn, str(f))

    assert state_before["sha256"] != state_after["sha256"]


# ---------------------------------------------------------------------------
# SyncHandler — on_deleted
# ---------------------------------------------------------------------------

def test_on_deleted_file(handler, conn, watch_dir):
    """on_deleted should remove the file's record from the DB."""
    f = watch_dir / "doomed.txt"
    f.write_text("bye")
    update_file_state(conn, str(f))

    event = FileDeletedEvent(str(f))
    handler.on_deleted(event)

    assert get_file_state(conn, str(f)) is None


def test_on_deleted_directory(handler, conn, watch_dir):
    """on_deleted for a directory should clear all descendant records."""
    sub = watch_dir / "subdir"
    sub.mkdir()
    f1 = sub / "a.txt"
    f2 = sub / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    update_file_state(conn, str(f1))
    update_file_state(conn, str(f2))

    event = DirDeletedEvent(str(sub))
    handler.on_deleted(event)

    assert get_file_state(conn, str(f1)) is None
    assert get_file_state(conn, str(f2)) is None


# ---------------------------------------------------------------------------
# SyncHandler — on_moved
# ---------------------------------------------------------------------------

def test_on_moved_file_inside(handler, conn, watch_dir):
    """Moving a file within the watch dir should repoint the DB record."""
    f = watch_dir / "old.txt"
    f.write_text("data")
    update_file_state(conn, str(f))

    new_path = str(watch_dir / "new.txt")
    event = FileMovedEvent(str(f), new_path)
    handler.on_moved(event)

    assert get_file_state(conn, str(f)) is None
    assert get_file_state(conn, new_path) is not None


def test_on_moved_file_outside(handler, conn, watch_dir, tmp_path):
    """Moving a file out of the watch dir should remove its record (treated as delete)."""
    f = watch_dir / "leaving.txt"
    f.write_text("data")
    update_file_state(conn, str(f))

    outside = str(tmp_path / "elsewhere" / "leaving.txt")
    event = FileMovedEvent(str(f), outside)
    handler.on_moved(event)

    assert get_file_state(conn, str(f)) is None
    assert get_file_state(conn, outside) is None  # not tracked — outside watch root


def test_on_moved_dir_inside(handler, conn, watch_dir):
    """Moving a directory within the watch dir should repoint all descendants."""
    old = watch_dir / "old_dir"
    old.mkdir()
    f = old / "file.txt"
    f.write_text("data")
    update_file_state(conn, str(f))

    new = watch_dir / "new_dir"
    event = DirMovedEvent(str(old), str(new))
    handler.on_moved(event)

    assert get_file_state(conn, str(f)) is None
    assert get_file_state(conn, str(new / "file.txt")) is not None


def test_on_moved_dir_outside(handler, conn, watch_dir, tmp_path):
    """Moving a directory out of the watch dir should clear all descendants."""
    sub = watch_dir / "going"
    sub.mkdir()
    f = sub / "file.txt"
    f.write_text("data")
    update_file_state(conn, str(f))

    outside = str(tmp_path / "trash" / "going")
    event = DirMovedEvent(str(sub), outside)
    handler.on_moved(event)

    assert get_file_state(conn, str(f)) is None


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

def test_reconcile_picks_up_new_files(conn, watch_dir):
    """Files on disk but not in the DB should be tracked after reconcile."""
    f = watch_dir / "untracked.txt"
    f.write_text("new")

    reconcile(conn, str(watch_dir))

    assert get_file_state(conn, str(f)) is not None


def test_reconcile_prunes_stale_records(conn, watch_dir):
    """DB records for deleted files should be removed after reconcile."""
    f = watch_dir / "temp.txt"
    f.write_text("data")
    update_file_state(conn, str(f))

    # Delete the file but leave the DB record
    os.remove(str(f))

    reconcile(conn, str(watch_dir))

    assert get_file_state(conn, str(f)) is None