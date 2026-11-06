"""Queue-safe snapshots and transactional application of alignment results."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from classcue.asr_models import (
    get_model, selected_model_id, selected_dolphin_device, selected_firered_device,
    selected_paraformer_device,
)
from classcue.paths import PROJECT_ROOT
from classcue.qwen_process import run_process
from classcue.secondary_alignment import MAX_RUNTIME_SECONDS, anchors
from classcue.secondary_alignment_worker import PREFIX

JOB_TYPE = "secondary_view_align"


def model_selection():
    model_id = selected_model_id()
    engine = get_model(model_id).engine
    device = {"dolphin": selected_dolphin_device, "firered": selected_firered_device,
              "paraformer": selected_paraformer_device}.get(engine)
    return {"id": model_id, "device": device() if device else "automatic"}


def file_identity(video):
    path = Path(video["video_path"])
    stat = path.stat()
    return [video["id"], video["content_hash"], str(path.resolve()), stat.st_size, stat.st_mtime_ns]


def subtitle_fingerprint(segments):
    return hashlib.sha256(json.dumps(
        [asdict(s) for s in segments], ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()


def alignment_snapshot(store, video_id, library_id):
    view = store.get_secondary_view(video_id)
    if view is None:
        raise ValueError("尚未导入副视角视频")
    segments = store.get_segments(video_id)
    if not anchors(segments):
        raise ValueError("主字幕缺少可对齐的长句")
    primary, secondary = store.get_video(video_id), store.get_video(view["video_id"])
    return {
        "video_id": video_id, "secondary_id": view["video_id"], "library_id": library_id,
        "original_offset_ms": view["offset_ms"], "duration_ms": secondary["duration_ms"],
        "primary_file": file_identity(primary), "secondary_file": file_identity(secondary),
        "subtitle_fingerprint": subtitle_fingerprint(segments), "model": model_selection(),
    }


def snapshot_current(store, snapshot, library_id):
    try:
        current = alignment_snapshot(store, snapshot["video_id"], library_id)
    except (ValueError, OSError, TypeError):
        return False
    return all(current[key] == snapshot[key] for key in (
        "video_id", "secondary_id", "library_id", "original_offset_ms",
        "primary_file", "secondary_file", "subtitle_fingerprint", "model",
    ))


def latest_alignment(store, video_id, secondary_id):
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT id, status, request_json FROM processing_jobs WHERE video_id = ? AND job_type = ? "
            "ORDER BY created_at DESC, rowid DESC", (video_id, JOB_TYPE),
        ).fetchall()
    for row in rows:
        record = json.loads(row["request_json"] or "{}").get("alignment", {})
        if record.get("snapshot", {}).get("secondary_id") == secondary_id:
            return row
    return None


def run_worker(store, snapshot, update, cancelled):
    directory = store.path.parent / "temporary_audio"
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="secondary-align-", dir=directory) as temporary:
        root = Path(temporary)
        request_path, result_path = root / "request.json", root / "result.json"
        request = {**snapshot, "secondary_path": snapshot["secondary_file"][2],
                   "segments": [asdict(s) for s in store.get_segments(snapshot["video_id"])]}
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}

        def on_line(line):
            if not line.startswith(PREFIX):
                return
            try:
                event = json.loads(line[len(PREFIX):])
                update(.2, event["message"], event["stage"])
            except (ValueError, KeyError):
                return

        try:
            code = run_process(
                [sys.executable, "-m", "classcue.secondary_alignment_worker", str(request_path), str(result_path)],
                cwd=PROJECT_ROOT, env=env, should_stop=cancelled, on_line=on_line,
                timeout=MAX_RUNTIME_SECONDS,
            )
        except subprocess.TimeoutExpired:
            code = 0
        if not result_path.exists():
            if code:
                raise ValueError("自动对齐进程未能完成，请检查 ASR 环境")
            return {"outcome": "no_match", "message": "达到运行时限，原偏移已保留", "offset_ms": None,
                    "evidence": []}
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("error") or code:
            raise ValueError(result.get("error") or "自动对齐进程异常退出，原偏移已保留")
        return result


def save_result(store, job_id, snapshot, result, *, apply=False):
    """Compare the association and persist result/offset atomically."""
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT request_json FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()
        record = json.loads(row["request_json"] or "{}")
        if apply:
            current = snapshot_current(store, snapshot, snapshot["library_id"])
            changed = current and connection.execute(
                "UPDATE video_secondary_views SET offset_ms = ? WHERE video_id = ? "
                "AND secondary_video_id = ? AND offset_ms = ?",
                (result["offset_ms"], snapshot["video_id"], snapshot["secondary_id"], snapshot["original_offset_ms"]),
            ).rowcount
            if changed:
                result = {**result, "outcome": "applied", "message": "已自动对齐，可撤销"}
            else:
                result = {**result, "outcome": "stale", "message": "副视角设置已变化，原结果未应用"}
        record["alignment"] = {"snapshot": snapshot, "result": result}
        connection.execute("UPDATE processing_jobs SET request_json = ? WHERE id = ?",
                           (json.dumps(record, ensure_ascii=False), job_id))
    return result


def change_result(store, job_id, video_id, library_id, action):
    row = store.get_job(job_id)
    if row is None or row["job_type"] != JOB_TYPE or row["video_id"] != video_id:
        raise KeyError("对齐任务不存在")
    record = json.loads(row["request_json"] or "{}")["alignment"]
    snapshot, result = record["snapshot"], record.get("result", {})
    if row["status"] != "completed":
        raise ValueError("请等待对齐任务完成")
    if action == "apply" and result.get("outcome") == "applied":
        expected = {**snapshot, "original_offset_ms": result["offset_ms"]}
        if snapshot_current(store, expected, library_id):
            return result
        raise ValueError("副视角设置已变化，请重新对齐")
    if action == "undo" and result.get("outcome") == "undone":
        if snapshot_current(store, snapshot, library_id):
            return result
        raise ValueError("副视角设置已变化，无法撤销旧结果")
    if action == "apply":
        if result.get("outcome") != "candidate" or not snapshot_current(store, snapshot, library_id):
            raise ValueError("候选结果已失效，请重新对齐")
        return save_result(store, job_id, snapshot, result, apply=True)
    if result.get("outcome") != "applied":
        raise ValueError("没有可撤销的对齐结果")
    expected = {**snapshot, "original_offset_ms": result["offset_ms"]}
    if not snapshot_current(store, expected, library_id):
        raise ValueError("偏移或视频已被修改，无法撤销旧结果")
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE video_secondary_views SET offset_ms = ? WHERE video_id = ? "
            "AND secondary_video_id = ? AND offset_ms = ?",
            (snapshot["original_offset_ms"], video_id, snapshot["secondary_id"], result["offset_ms"]),
        ).rowcount
        if not changed:
            raise ValueError("偏移已被修改，无法撤销旧结果")
        result = {**result, "outcome": "undone", "message": "已恢复对齐前的偏移"}
        record["result"] = result
        request = json.loads(row["request_json"])
        request["alignment"] = record
        connection.execute("UPDATE processing_jobs SET request_json = ? WHERE id = ?",
                           (json.dumps(request, ensure_ascii=False), job_id))
    return result
