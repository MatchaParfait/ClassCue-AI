"""Opt-in physical USB acceptance. Uses only synthetic media and the QA tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import av
import numpy as np

from classcue.core import TranscriptSegment
from classcue.device_transfers import TransferManager
from classcue.device_wpd import WpdTransport
from classcue.study_sync import StudySyncStore, digest, records
from classcue.video_store import VideoStore


def seed(directory):
    store = VideoStore(directory / "library" / "project.db")
    videos = []
    for number, label in enumerate(("A", "B")):
        path = directory / f"synthetic-{label}.mp4"
        with av.open(str(path), "w") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
            for index in range(20):
                frame = av.VideoFrame.from_ndarray(np.full((240, 320, 3), (index * 9 + number * 35) % 255, dtype=np.uint8), format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        video = store.create_video(store.file_hash(path), f"Synthetic {label}", str(path), "", 2000,
                                   source_size=path.stat().st_size, source_mtime_ns=path.stat().st_mtime_ns)
        store.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle"),
                                       TranscriptSegment(2, 1000, 2000, "Second synthetic subtitle")], "synthetic")
        store.set_analysis_run(video, "medium_recall", "completed", 2)
        store.add_marker(video, 100, 500, "Synthetic point 1", "Original note", "Synthetic subtitle", "Trigger 1", .9, "medium_recall")
        store.add_marker(video, 1100, 1600, "Synthetic point 2", "", "Second synthetic subtitle", "Trigger 2", 1, "medium_recall")
        store.create_manual_question(video, 600)
        store.set_analysis_run(video, "question_extraction", "completed", 1)
        store.set_status(video, "review_ready")
        videos.append(video)
    return store, videos


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=args.resume)
    if args.resume:
        store = VideoStore(directory / "library" / "project.db")
        videos = [row["id"] for row in sorted(store.list_videos(), key=lambda row: row["filename"])]
    else:
        store, videos = seed(directory)

    def manager():
        return TransferManager(lambda: store, directory / "transfers", lambda: directory / "videos",
                               lambda: directory / "audio", transport=WpdTransport(qa=True))

    local = manager()
    started = time.monotonic()
    outcomes = []

    def log(message):
        print(message, flush=True)
        outcomes.append({"elapsed": round(time.monotonic() - started, 1), "message": message})

    def wait(description, predicate, *, ticks=True, timeout=150):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ticks:
                local.tick()
            result = predicate()
            if result:
                log(description)
                return result
            time.sleep(.6)
        tasks = [{key: task.get(key) for key in ("id", "direction", "state", "error", "preview")} for task in StudySyncStore(store).tasks()]
        raise AssertionError(f"Timed out: {description}; sync={tasks}; error={local.discovery_error}")

    def state():
        content = local._read(device["id"], "qa/state.json")
        return json.loads(content) if content else {}

    sequence = 0

    def command(action):
        nonlocal sequence
        sequence += 1
        local.transport.write(device["id"], "qa/control.json", json.dumps({"sequence": sequence, "action": action}).encode())
        return wait(f"Pad command {action} completed", lambda: state().get("command") == sequence)

    def pending():
        return [task for task in StudySyncStore(store).tasks() if task["state"] not in {"completed", "rejected"}]

    def accept_pending():
        for task in pending():
            if task["state"] == "awaiting_confirmation":
                assert not task["preview"]["conflicts"], task
                local.sync.accept(task["id"], {})
        return not pending()

    try:
        device = wait("QA Pad discovered over WPD", lambda: next((d for d in local.devices_cache if d.get("root_available") and d.get("foreground")), None))
        pair = local.request_pairing(device["id"])
        local.confirm_pairing(pair["id"])
        wait("Both P-256 pairing confirmations received", lambda: local.journal.peer(device["peer_id"]))
        wait("Paired device discovery refreshed", lambda: any(d.get("paired") for d in local.devices_cache))
        local.transport.write(device["id"], "sync/00000000-0000-0000-0000-000000000002/request.json", b"{broken")
        sent = next((task for task in local.journal.tasks() if task["direction"] == "send"), None) if args.resume else None
        sent = sent or local.create_send(device["id"], video_ids=videos)
        wait("Two synthetic videos and v1 subtitles imported over MTP", lambda: local.journal.task(sent["id"])["state"] == "completed", timeout=300)
        assert all(item["state"] in {"imported", "skipped"} for item in local.journal.task(sent["id"])["results"])
        local.sync.start(device["id"], videos)
        wait("Initial points and questions linked on both peers", accept_pending)
        command("edit")
        # Work on another video on PC while the tablet's first video has offline edits.
        pc_marker = store.list_markers(videos[1], "medium_recall")[0]
        store.set_marker_review_status(pc_marker["id"], "excluded")
        pc_question = store.create_manual_question(videos[1], 1200)
        store.rename_manual_question(pc_question["id"], "PC added question")
        command("sync")
        wait("Tablet incoming changes discovered", lambda: any(t["direction"] == "incoming" for t in pending()))
        wait("Tablet edits and other-video PC changes merged", accept_pending)
        first = StudySyncStore(store).export(videos[0])
        assert any(r["title"] == "Tablet corrected AI point" and r["note"] == "Offline tablet note" for r in first["records"])
        assert any(r["title"] == "Tablet added point" and r["origin"] == "manual" for r in first["records"])
        assert any(r["title"] == "Tablet added question" and not r["removed"] for r in first["records"])
        assert any(r["title"] == "Tablet removed question" and r["removed"] for r in first["records"])
        assert any(r["title"] == "Synthetic point 2" and r["removed"] for r in first["records"])
        command("inspect")
        documents = json.loads(local.transport.read(device["id"], "qa/documents.json"))
        for document in documents:
            video = store.get_video_by_hash(document["video_sha256"])
            assert StudySyncStore(store).export(video["id"]) == document
        log("Both independent libraries match including tombstones and questions")

        command("conflict")
        with store.connect() as connection:
            connection.execute("UPDATE video_markers SET title='PC conflicting title' WHERE video_id=? AND title='Tablet corrected AI point'", (videos[0],))
        command("sync")
        conflicting = wait("Same-field conflict is visible on Windows", lambda: next((t for t in pending() if t.get("preview", {}).get("conflicts")), None))
        assert conflicting["state"] == "awaiting_confirmation"
        choices = {row["id"]: "remote" for row in conflicting["preview"]["conflicts"]}
        local.sync.accept(conflicting["id"], choices)
        wait("Explicit tablet choice converged", accept_pending)
        assert any(r["title"] == "Tablet conflict choice" for r in StudySyncStore(store).export(videos[0])["records"])

        # The same request must survive a missing receipt and a Windows process restart.
        command("restore")
        command("sync")
        incoming = wait("Restore request arrived", lambda: next((t for t in pending() if t["video_id"] == videos[0]), None))
        local.sync.accept(incoming["id"], {})
        local.tick()
        local.transport.delete(device["id"], f"sync/{incoming['id']}/receipt.json")
        before = StudySyncStore(store).export(videos[0])
        local.close()
        local = manager()
        wait("Windows restart and lost receipt recovered using original IDs", accept_pending)
        assert StudySyncStore(store).export(videos[0]) == before
        assert len(records(before)) == len(before["records"])
        assert not any(r["removed"] for r in before["records"])
        command("inspect")
        documents = json.loads(local.transport.read(device["id"], "qa/documents.json"))
        for document in documents:
            video = store.get_video_by_hash(document["video_sha256"])
            assert document == StudySyncStore(store).export(video["id"])
        log("Restores persisted, retry produced no duplicates")
        command("stop")
        (directory / "result.json").write_text(json.dumps({"passed": True, "checks": outcomes}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        local.close()


if __name__ == "__main__":
    main()
