"""Durable transfer journal, separate from ASR/AI processing queues."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from classcue.sqlite_connection import ClosingConnection


class TransferStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "journal.sqlite"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS peers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, public_key TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pairs (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, video_id INTEGER NOT NULL, body TEXT NOT NULL,
                    created REAL NOT NULL, restored INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS folders (
                    peer TEXT NOT NULL, course TEXT NOT NULL, source TEXT NOT NULL, target INTEGER NOT NULL,
                    PRIMARY KEY(peer,course,source));
            """)

    def connect(self):
        connection = sqlite3.connect(self.path, factory=ClosingConnection, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def peers(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM peers ORDER BY name")]

    def peer(self, identity):
        return next((row for row in self.peers() if row["id"] == identity), None)

    def trust(self, identity, name, key):
        with self.connect() as connection:
            existing = connection.execute("SELECT public_key FROM peers WHERE id=?", (identity,)).fetchone()
            if existing and existing[0] != key:
                raise ValueError("配对设备公钥已改变")
            connection.execute("INSERT OR REPLACE INTO peers VALUES(?,?,?)", (identity, name, key))

    def pair(self, identity):
        with self.connect() as connection:
            row = connection.execute("SELECT body FROM pairs WHERE id=?", (identity,)).fetchone()
            return json.loads(row[0]) if row else None

    def pairs(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM pairs")]

    def save_pair(self, value):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO pairs VALUES(?,?)", (value["id"], json.dumps(value)))

    def tasks(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM tasks ORDER BY updated DESC")]

    def task(self, identity):
        with self.connect() as connection:
            row = connection.execute("SELECT body FROM tasks WHERE id=?", (identity,)).fetchone()
            if not row:
                raise ValueError("传输任务不存在")
            return json.loads(row[0])

    def save(self, task):
        task["updated"] = time.time()
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)",
                               (task["id"], json.dumps(task, ensure_ascii=False), task["updated"]))

    def snapshot(self, video_id, value):
        identity = str(uuid4())
        with self.connect() as connection:
            connection.execute("INSERT INTO snapshots VALUES(?,?,?,?,0)",
                               (identity, video_id, json.dumps(value, ensure_ascii=False), time.time()))
        return identity

    def snapshots(self, library_path=None):
        with self.connect() as connection:
            connection.execute("DELETE FROM snapshots WHERE created<?", (time.time() - 7 * 86400,))
            result = []
            for row in connection.execute(
                    "SELECT * FROM snapshots WHERE created>? ORDER BY created DESC", (time.time() - 7 * 86400,)):
                if library_path is not None and json.loads(row["body"]).get("library_path") != library_path:
                    continue
                result.append({key: row[key] for key in ("id", "video_id", "created", "restored")})
            return result

    def snapshot_value(self, identity):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE id=? AND created>?",
                                     (identity, time.time() - 7 * 86400)).fetchone()
            if row is None:
                raise ValueError("覆盖备份不存在或已过期")
            return dict(row)

    def restored(self, identity):
        with self.connect() as connection:
            connection.execute("UPDATE snapshots SET restored=1 WHERE id=?", (identity,))

    def folder(self, peer, course, source):
        with self.connect() as connection:
            row = connection.execute("SELECT target FROM folders WHERE peer=? AND course=? AND source=?",
                                     (peer, course, source)).fetchone()
            return row[0] if row else None

    def save_folder(self, peer, course, source, target):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO folders VALUES(?,?,?,?)", (peer, course, source, target))
