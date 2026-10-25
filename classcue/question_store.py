"""Question material and analysis history, scoped to an existing marker."""

from __future__ import annotations

import json
import uuid

from classcue.bilibili_store import BilibiliStore, digest, encode
from classcue.question_analysis import LIGHT_VERSION, VERSION, QuestionMaterial
from classcue.question_captures import QuestionCaptures, initialize_captures


def initialize_questions(connection):
    initialize_captures(connection)
    connection.execute("""CREATE TABLE IF NOT EXISTS question_materials (
        marker_id INTEGER PRIMARY KEY REFERENCES video_markers(id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS question_analyses (
        id TEXT PRIMARY KEY,
        marker_id INTEGER NOT NULL REFERENCES video_markers(id) ON DELETE CASCADE,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        input_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS question_by_marker ON question_analyses(marker_id, created_at)")


class QuestionStore:
    def __init__(self, store):
        self.store = store

    def material(self, marker_id: int) -> dict:
        BilibiliStore(self.store).marker(marker_id)
        with self.store.connect() as connection:
            row = connection.execute("SELECT payload_json FROM question_materials WHERE marker_id = ?", (marker_id,)).fetchone()
        return QuestionMaterial.model_validate(json.loads(row[0]) if row else {}).model_dump()

    def save_material(self, marker_id: int, material: QuestionMaterial) -> dict:
        marker = BilibiliStore(self.store).marker(marker_id)
        for slide_id in material.slide_ids:
            slide = self.store.get_slide(slide_id)
            if slide is None or slide["video_id"] != marker["video_id"]:
                raise ValueError("所选 PPT 不属于当前课堂视频")
        for capture_id in material.capture_ids:
            try:
                QuestionCaptures(self.store).get(marker_id, capture_id)
            except KeyError:
                raise ValueError("所选截图不属于当前考点") from None
        value = material.model_dump()
        with self.store.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO question_materials(marker_id, payload_json) VALUES (?, ?)",
                               (marker_id, encode(value)))
        return value

    def snapshot(self, marker_id: int, classroom: dict | None = None) -> dict:
        if classroom is None:
            classroom = BilibiliStore(self.store).snapshot(marker_id, include_question=False)
        material = self.material(marker_id)
        images = []
        for slide_id in material["slide_ids"]:
            slide = self.store.get_slide(slide_id)
            if slide is None or slide["video_id"] != classroom["marker"]["video_id"]:
                images.append({"source": f"slide:{slide_id}", "slide_id": slide_id, "missing": True})
                continue
            images.append({"source": f"slide:{slide_id}", "slide_id": slide_id,
                           **{key: slide[key] for key in (
                               "image_hash", "start_ms", "end_ms", "representative_ms",
                               "visible_from_ms", "visible_until_ms", "page_start_ms", "page_end_ms",
                           )}})
        for capture_id in material["capture_ids"]:
            try:
                capture = QuestionCaptures(self.store).get(marker_id, capture_id)
            except KeyError:
                images.append({"source": f"capture:{capture_id}", "capture_id": capture_id, "missing": True})
                continue
            images.append({"source": f"capture:{capture_id}", "capture_id": capture_id,
                           "image_hash": capture["image_hash"], "captured_at_ms": capture["captured_at_ms"],
                           "start_ms": capture["captured_at_ms"], "end_ms": capture["captured_at_ms"],
                           "representative_ms": capture["captured_at_ms"], "kind": "player_capture"})
        snapshot = {"version": VERSION, "classroom": classroom,
                    "question": {"text": material["text"], "images": images}}
        if material["prompt_style"] == "light":
            snapshot.update(version=LIGHT_VERSION, prompt_style="light")
        return snapshot

    def create_run(self, snapshot: dict, profile: dict) -> dict:
        run = {"id": uuid.uuid4().hex, "job_id": uuid.uuid4().hex, "version": snapshot["version"],
               "input": snapshot, "input_hash": digest(snapshot), "profile": profile,
               "status": "queued", "analysis": None, "target": None, "error": ""}
        marker = snapshot["classroom"]["marker"]
        with self.store.connect() as connection:
            connection.execute("""INSERT INTO question_analyses(id, marker_id, video_id, input_hash, payload_json)
                VALUES (?, ?, ?, ?, ?)""", (run["id"], marker["id"], marker["video_id"], run["input_hash"], encode(run)))
        return run

    def save_run(self, run: dict):
        with self.store.connect() as connection:
            connection.execute("UPDATE question_analyses SET payload_json = ? WHERE id = ?", (encode(run), run["id"]))

    def runs(self, marker_id: int, snapshot: dict | None = None) -> list[dict]:
        snapshot = snapshot if snapshot is not None else self.snapshot(marker_id)
        fingerprint = digest(snapshot)
        with self.store.connect() as connection:
            rows = connection.execute("""SELECT a.payload_json, j.status AS job_status FROM question_analyses a
                LEFT JOIN processing_jobs j ON j.id = json_extract(a.payload_json, '$.job_id')
                WHERE a.marker_id = ? ORDER BY a.rowid DESC LIMIT 20""", (marker_id,)).fetchall()
        result = []
        for row in rows:
            run = json.loads(row["payload_json"])
            run["stale"] = run["input_hash"] != fingerprint or run["version"] != snapshot["version"]
            if run["status"] in {"queued", "analyzing"} and row["job_status"] not in {"queued", "running"}:
                run["status"] = "interrupted"
            result.append(run)
        return result

    def search_projection(self, marker_id: int, classroom: dict) -> dict | None:
        material = self.material(marker_id)
        snapshot = self.snapshot(marker_id, classroom)
        runs = self.runs(marker_id, snapshot)
        if not runs and not material["text"] and not material["slide_ids"] and not material["capture_ids"]:
            return None
        current = next((run for run in runs if not run["stale"] and run["status"] == "completed"), None)
        if current is None:
            return {"status": "needs_analysis", "input_hash": digest(snapshot)}
        projection = {"status": "ready" if current["target"] else "needs_clarification",
                      "analysis_id": current["id"], "input_hash": current["input_hash"],
                      "analysis": current["analysis"], "target": current["target"]}
        if current["analysis"].get("prompt_style") == "light":
            projection["images"] = current["input"]["question"]["images"]
        return projection
