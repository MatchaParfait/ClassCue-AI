import sqlite3

import pytest

from db import Database
from sqlite_connection import ClosingConnection
from video_store import VideoStore


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("store_type", [Database, VideoStore])
def test_store_connections_commit_close_and_leave_rows_readable(tmp_path, store_type):
    store = store_type(tmp_path / "test.db")
    with store.connect() as connection:
        assert isinstance(connection, ClosingConnection)
        connection.execute("CREATE TABLE example (value INTEGER)")
        connection.execute("INSERT INTO example VALUES (7)")
        row = connection.execute("SELECT value FROM example").fetchone()
    assert_closed(connection)
    assert row["value"] == 7
    with store.connect() as reader:
        assert reader.execute("SELECT value FROM example").fetchone()["value"] == 7
    assert_closed(reader)


def test_exception_rolls_back_and_closes(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path, factory=ClosingConnection) as setup:
        setup.execute("CREATE TABLE example (value INTEGER)")
    connection = sqlite3.connect(path, factory=ClosingConnection)
    with pytest.raises(RuntimeError, match="injected"):
        with connection:
            connection.execute("INSERT INTO example VALUES (7)")
            raise RuntimeError("injected")
    assert_closed(connection)
    with sqlite3.connect(path, factory=ClosingConnection) as reader:
        assert reader.execute("SELECT COUNT(*) FROM example").fetchone()[0] == 0


def test_failed_commit_also_closes_and_rolls_back(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path, factory=ClosingConnection) as setup:
        setup.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) "
            "DEFERRABLE INITIALLY DEFERRED);"
        )
    connection = sqlite3.connect(path, factory=ClosingConnection)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            connection.execute("INSERT INTO child VALUES (999)")
    assert_closed(connection)
    with sqlite3.connect(path, factory=ClosingConnection) as reader:
        assert reader.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
