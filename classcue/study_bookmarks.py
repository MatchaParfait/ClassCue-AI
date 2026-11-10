"""Persistent playback history and user reminders, independent of AI results."""

from __future__ import annotations

import time


def initialize_study_bookmarks(db):
    db.execute("""CREATE TABLE IF NOT EXISTS video_study_progress (
        video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
        position_ms INTEGER NOT NULL CHECK(position_ms >= 0),
        duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0),
        observed_at INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS video_bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        position_ms INTEGER CHECK(position_ms >= 0),
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("CREATE INDEX IF NOT EXISTS bookmarks_by_video ON video_bookmarks(video_id, id)")


class StudyBookmarkStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _video(db, video_id):
        row = db.execute("SELECT * FROM videos WHERE id=? AND is_secondary=0", (video_id,)).fetchone()
        if row is None:
            raise KeyError("视频不存在")
        return row

    def progress(self, video_id):
        with self.store.connect() as db:
            self._video(db, video_id)
            row = db.execute("SELECT * FROM video_study_progress WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    def save_progress(self, video_id, position_ms, duration_ms, observed_at):
        with self.store.connect() as db:
            video = self._video(db, video_id)
            duration_ms = video["duration_ms"] or duration_ms
            if duration_ms > 0:
                position_ms = min(position_ms, duration_ms)
            db.execute("""INSERT INTO video_study_progress(video_id,position_ms,duration_ms,observed_at)
                VALUES(?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
                position_ms=excluded.position_ms,duration_ms=excluded.duration_ms,
                observed_at=excluded.observed_at,updated_at=CURRENT_TIMESTAMP
                WHERE excluded.observed_at >= video_study_progress.observed_at""",
                       (video_id, position_ms, duration_ms, min(observed_at, int(time.time() * 1000))))
            row = db.execute("SELECT * FROM video_study_progress WHERE video_id=?", (video_id,)).fetchone()
        return dict(row)

    def create(self, video_id, position_ms, note):
        with self.store.connect() as db:
            video = self._video(db, video_id)
            if position_ms is not None and video["duration_ms"] > 0 and position_ms > video["duration_ms"]:
                raise ValueError("书签时间超出视频时长")
            cursor = db.execute("INSERT INTO video_bookmarks(video_id,position_ms,note) VALUES(?,?,?)",
                                (video_id, position_ms, note.strip()))
            row = db.execute("SELECT * FROM video_bookmarks WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def edit(self, bookmark_id, note):
        with self.store.connect() as db:
            cursor = db.execute("UPDATE video_bookmarks SET note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (note.strip(), bookmark_id))
            if not cursor.rowcount:
                raise KeyError("书签不存在")
            row = db.execute("SELECT * FROM video_bookmarks WHERE id=?", (bookmark_id,)).fetchone()
        return dict(row)

    def delete(self, bookmark_id):
        with self.store.connect() as db:
            if not db.execute("DELETE FROM video_bookmarks WHERE id=?", (bookmark_id,)).rowcount:
                raise KeyError("书签不存在")

    def _paths(self, db):
        folders = {row["id"]: dict(row) for row in db.execute("SELECT id,name,parent_id FROM video_folders")}
        paths = {}
        for folder_id in folders:
            path, seen, current = [], set(), folder_id
            while current in folders and current not in seen:
                seen.add(current)
                folder = folders[current]
                path.append({"id": current, "name": folder["name"]})
                current = folder["parent_id"]
            paths[folder_id] = path[::-1]
        return folders, paths

    def navigation(self):
        with self.store.connect() as db:
            folders, paths = self._paths(db)
            rows = db.execute("""SELECT v.id video_id,v.display_name,v.filename,v.library_folder_id folder_id,
                v.duration_ms,v.status,
                p.position_ms,p.duration_ms watched_duration_ms,p.observed_at,
                (SELECT COUNT(*) FROM video_bookmarks b WHERE b.video_id=v.id) bookmark_count,
                (SELECT note FROM video_bookmarks b WHERE b.video_id=v.id ORDER BY b.id DESC LIMIT 1) bookmark_note
                FROM videos v LEFT JOIN video_study_progress p ON p.video_id=v.id
                WHERE v.is_secondary=0""").fetchall()
        summaries = {folder_id: {"folder_id": folder_id, "bookmark_count": 0, "latest": None}
                     for folder_id in folders}
        videos, latest = [], None
        for row in rows:
            item = dict(row)
            item["path"] = paths.get(item["folder_id"], [])
            duration = item["duration_ms"] or item["watched_duration_ms"] or 0
            item["duration_ms"] = duration
            item["completed"] = item["position_ms"] is not None and duration > 0 and (
                item["position_ms"] >= duration - min(3000, duration * .02)
            )
            videos.append(item)
            if item["observed_at"] is not None and (
                latest is None or (item["observed_at"], item["video_id"]) > (latest["observed_at"], latest["video_id"])
            ):
                latest = item
            for folder in item["path"]:
                summary = summaries[folder["id"]]
                summary["bookmark_count"] += item["bookmark_count"]
                previous = summary["latest"]
                if item["observed_at"] is not None and (
                    previous is None or (item["observed_at"], item["video_id"]) > (previous["observed_at"], previous["video_id"])
                ):
                    summary["latest"] = item
        return {"videos": videos, "folders": list(summaries.values()), "latest": latest}

    def bookmarks(self, folder_id=None, video_id=None):
        with self.store.connect() as db:
            folders, paths = self._paths(db)
            if folder_id is not None and folder_id not in folders:
                raise KeyError("文件夹不存在")
            if video_id is not None:
                self._video(db, video_id)
            parameters = () if video_id is None else (video_id,)
            rows = db.execute("""SELECT b.*,v.display_name,v.filename,v.library_folder_id folder_id,v.status,v.duration_ms,
                EXISTS(SELECT 1 FROM video_transcript_segments t WHERE t.video_id=v.id) has_transcript
                FROM video_bookmarks b JOIN videos v ON v.id=b.video_id WHERE v.is_secondary=0"""
                              + ("" if video_id is None else " AND b.video_id=?") + " ORDER BY b.id DESC",
                              parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["path"] = paths.get(item["folder_id"], [])
            if folder_id is None or any(folder["id"] == folder_id for folder in item["path"]):
                result.append(item)
        return result
