from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from classcue import device_protocol as wire, study_share
from classcue.core import TranscriptSegment
from classcue.device_study import current_digest, import_study, restore_study
from classcue.device_transfer_store import TransferStore
from classcue.device_transfers import TransferManager
import classcue.device_transfers as transfer_module
from classcue.device_wpd import FolderTransport, relative_path
from classcue.video_store import VideoStore

FIXTURE = Path(__file__).parent / "fixtures" / "device-transfer-v1.studyshare"


def synthetic_store(root, *, studied=False):
    store = VideoStore(root / "library.sqlite")
    video = root / "synthetic.mp4"
    video.write_bytes(b"synthetic video bytes")
    identity = store.create_video(store.file_hash(video), video.name, str(video), "", 10000,
                                  source_size=video.stat().st_size, source_mtime_ns=video.stat().st_mtime_ns)
    if studied:
        store.mark_transcribed(identity, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle")], "synthetic-asr")
        store.set_analysis_run(identity, "medium_recall", "completed", 1)
        store.set_analysis_run(identity, "low_recall", "completed", 0)
        store.add_marker(identity, 0, 1000, "Synthetic marker", "Note", "Synthetic subtitle", "Important", .9, "medium_recall")
        store.set_status(identity, "review_ready")
    return store, identity


def test_semantic_vector_normalizes_numbers_custom_modes_and_export_time():
    content = FIXTURE.read_bytes()
    assert wire.semantic_digest(content) == "7315106680127a55ebd4c646614a413d9bc33704160f97d29ea5cfde6910806d"
    assert wire.canonical({"negative": -0.0, "x": Decimal("1E-7")}) == b'{"negative":0,"x":0.0000001}'
    assert wire.canonical(Decimal("0.123456789012345678901234567890100")) == b"0.1234567890123456789012345678901"
    changed = json.loads(content)
    changed["exported_at"] = "another export"
    changed["analysis_runs"][0]["analysis_mode"] = "custom_another"
    changed["analysis_runs"][0]["scheme_revision"] = 90
    changed["markers"][0]["analysis_mode"] = "custom_another"
    assert wire.semantic_digest(json.dumps(changed).encode()) == wire.semantic_digest(content)
    changed["markers"][0]["importance"] = "critical"
    assert wire.semantic_digest(json.dumps(changed).encode()) != wire.semantic_digest(content)


def test_signatures_bind_raw_bytes_key_peer_task_and_message_type():
    left = ec.generate_private_key(ec.SECP256R1())
    right = ec.generate_private_key(ec.SECP256R1())
    recipient = wire.peer_id(wire.public_key(right))
    content = wire.envelope(left, "offer", "task-1", recipient, {"text": "Synthetic<>&"})
    body, public = wire.open_envelope(content, recipient=recipient, message_id="task-1", message_type="offer")
    assert body["data"]["text"] == "Synthetic<>&"
    assert public == wire.public_key(left)
    for kwargs in ({"recipient": "another"}, {"message_id": "another"}, {"key": wire.public_key(right)}, {"message_type": "receipt"}):
        with pytest.raises(ValueError):
            wire.open_envelope(content, **kwargs)
    modified = json.loads(content)
    modified["payload"] = modified["payload"][:-4] + "AAAA"
    with pytest.raises(Exception):
        wire.open_envelope(json.dumps(modified).encode())
    assert wire.pairing_code(wire.public_key(left), wire.public_key(right), "nonce") == wire.pairing_code(wire.public_key(right), wire.public_key(left), "nonce")


def test_transfer_replacement_is_explicit_recoverable_and_rechecks_confirmation(tmp_path):
    source, sid = synthetic_store(tmp_path / "source", studied=True)
    target, tid = synthetic_store(tmp_path / "target")
    journal = TransferStore(tmp_path / "transfers")
    original = study_share.export_package(source, sid)
    assert import_study(target, journal, tid, original) is None
    initial = current_digest(target, tid)
    changed = json.loads(original)
    changed["markers"][0]["importance"] = "critical"
    changed["markers"][0]["user_start_ms"] = 300
    content = json.dumps(changed).encode()
    with pytest.raises(ValueError, match="覆盖"):
        import_study(target, journal, tid, content, expected_digest=initial)
    with pytest.raises(ValueError, match="改变"):
        import_study(target, journal, tid, content, overwrite=True, expected_digest="stale")
    snapshot = import_study(target, journal, tid, content, overwrite=True, expected_digest=initial)
    assert snapshot
    assert target.list_markers(tid)[0]["importance"] == "critical"
    restore_study(target, journal, snapshot)
    assert current_digest(target, tid) == initial
    assert target.list_markers(tid)[0]["importance"] == "standard"
    # The ordinary share endpoint retains its historical no-overwrite behavior.
    with pytest.raises(study_share.ShareError):
        study_share.import_package(target, study_share.decode_package(content),
                                  expected_hash=study_share.package_digest(study_share.decode_package(content)),
                                  expected_video_id=tid)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "tasks/../other", "tasks\\other", "./same", "bad\0name"])
def test_transport_paths_are_scoped(path):
    with pytest.raises(ValueError):
        relative_path(path)


def test_sender_waits_for_signed_receipt_and_does_not_resend_acknowledged_blocks(tmp_path):
    store, video_id = synthetic_store(tmp_path / "library", studied=True)
    root = tmp_path / "usb"
    transport = FolderTransport(root)
    remote_key = ec.generate_private_key(ec.SECP256R1())
    remote_public = wire.public_key(remote_key)
    remote_id = wire.peer_id(remote_public)
    transport.write("synthetic", "identity.json", json.dumps({
        "peer_id": remote_id, "public_key": remote_public, "name": "Synthetic Android", "foreground": True,
    }).encode())
    local = TransferManager(lambda: store, tmp_path / "journal", lambda: tmp_path / "videos",
                            lambda: tmp_path / "audio", transport=transport,
                            identity=ec.generate_private_key(ec.SECP256R1()))
    local.journal.trust(remote_id, "Synthetic Android", remote_public)
    local.scan()
    created = local.create_send("synthetic", video_ids=[video_id])
    task_id = created["id"]
    task = local.journal.task(task_id)
    item = task["offer"]["items"][0]
    decision = {"accepted": True, "items": [{"id": item["id"], "action": "receive", "need_video": True}],
                "target_folder_id": None}
    transport.write("synthetic", f"tasks/{task_id}/decision.json",
                    wire.envelope(remote_key, "decision", task_id, local.peer_id, decision))
    local.tick()
    stem = f"tasks/{task_id}/chunks/{item['id']}.000000"
    assert (root / (stem + ".bin")).read_bytes() == b"synthetic video bytes"
    ready, _ = wire.open_envelope((root / (stem + ".ready.json")).read_bytes(), message_type="block")
    assert ready["data"]["sha256"] == item["blocks"][0]["sha256"]
    # Merely copying the complete bytes is not equivalent to receiver import.
    assert local.journal.task(task_id)["state"] == "sending"
    status = {"state": "receiving", "acks": [{"id": item["id"], "index": 0}], "completed": [], "errors": [], "updated_at": wire.now()}
    transport.write("synthetic", f"tasks/{task_id}/status.json",
                    wire.envelope(remote_key, "status", task_id, local.peer_id, status))
    local.tick()
    assert not (root / (stem + ".bin")).exists()
    assert local.journal.task(task_id)["transferred_bytes"] == item["size"]
    receipt = {"state": "completed", "items": [{"id": item["id"], "state": "imported", "video_id": "synthetic",
                "subtitle_count": 1, "marker_count": 1, "message": "Imported"}], "completed_at": wire.now()}
    transport.write("synthetic", f"tasks/{task_id}/receipt.json",
                    wire.envelope(remote_key, "receipt", task_id, local.peer_id, receipt))
    local.tick()
    assert local.journal.task(task_id)["state"] == "completed"


def incoming_receiver(root, *, has_video=True):
    source, source_id = synthetic_store(root / "source", studied=True)
    if has_video:
        target, target_id = synthetic_store(root / "target")
    else:
        target, target_id = VideoStore(root / "target" / "library.sqlite"), None
    content = study_share.export_package(source, source_id)
    package = study_share.decode_package(content)
    video = source.get_video(source_id)
    identity = video["content_hash"]
    item = {"id": identity, "name": "Synthetic incoming", "size": video["source_size"],
            "duration_ms": 10000, "mime": "video/mp4", "folder_id": None,
            "blocks": [{"index": 0, "size": video["source_size"], "sha256": identity}],
            "study": {"size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                      "semantic_digest": wire.semantic_digest(content),
                      "subtitle_count": 1, "marker_count": 1,
                      "runs": [{"mode": run.analysis_mode, "name": run.name,
                                "count": sum(marker.analysis_mode == run.analysis_mode for marker in package.markers)}
                               for run in package.analysis_runs]}}
    remote_key = ec.generate_private_key(ec.SECP256R1())
    remote_public = wire.public_key(remote_key)
    remote_id = wire.peer_id(remote_public)
    transport = FolderTransport(root / "usb")
    transport.write("synthetic", "identity.json", json.dumps({
        "peer_id": remote_id, "public_key": remote_public, "name": "Synthetic Android", "foreground": True,
    }).encode())
    local = TransferManager(lambda: target, root / "journal", lambda: root / "videos",
                            lambda: root / "audio", transport=transport,
                            identity=ec.generate_private_key(ec.SECP256R1()))
    local.journal.trust(remote_id, "Synthetic Android", remote_public)
    task_id = str(uuid4())
    offer = {"title": "Synthetic incoming", "course_id": "synthetic", "folders": [], "items": [item], "total_bytes": item["size"]}
    transport.write("synthetic", f"tasks/{task_id}/offer.json",
                    wire.envelope(remote_key, "offer", task_id, local.peer_id, offer))
    local.tick()
    local.accept(task_id, [{"id": identity, "action": "receive"}])
    transport.write("synthetic", f"tasks/{task_id}/studies/{identity}.studyshare", content)
    return local, target, target_id, transport, remote_key, task_id, item


def test_receiver_requires_study_commit_marker_and_recovers_library_commit_before_receipt(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    local.tick()
    assert target.get_video(target_id)["status"] != "review_ready"
    transport.write("synthetic", f"tasks/{task_id}/studies/{item['id']}.ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    local.tick()
    assert current_digest(target, target_id) == item["study"]["semantic_digest"]
    # Simulate process death after the library transaction, before task-journal commit.
    task = local.journal.task(task_id)
    task["results"] = []
    task["completed"] = []
    local.journal.save(task)
    transport.delete("synthetic", f"tasks/{task_id}/studies/{item['id']}.studyshare")
    local.tick()
    assert len(local.journal.task(task_id)["results"]) == 1
    assert len(target.list_markers(target_id)) == 1
    local.tick()
    assert local.journal.task(task_id)["state"] == "completed"
    receipt = local._read("synthetic", f"tasks/{task_id}/receipt.json")
    body, _ = wire.open_envelope(receipt, message_type="receipt")
    assert body["data"]["state"] == "completed"


def test_receiver_corrupt_committed_study_requests_retransmission_without_touching_library(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    path = f"tasks/{task_id}/studies/{item['id']}"
    transport.write("synthetic", path + ".ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    transport.write("synthetic", path + ".studyshare", b"partial contents")
    local.tick()
    assert not transport._path(path + ".ready.json").exists()
    assert not transport._path(path + ".studyshare").exists()
    assert not target.list_markers(target_id)
    assert local.journal.task(task_id)["state"] == "receiving"


def test_cancelled_receiver_persists_and_republishes_receipt(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    stage = local.journal.directory / "tasks" / task_id / f"{item['id']}.mp4"
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_bytes(b"uncommitted")
    local.control(task_id, "cancel")
    assert not stage.exists()
    assert Path(target.get_video(target_id)["video_path"]).exists()
    assert local.journal.task(task_id)["pending_receipt"] is True
    local.tick()
    body, _ = wire.open_envelope(transport.read("synthetic", f"tasks/{task_id}/receipt.json"), message_type="receipt")
    assert body["data"]["state"] == "cancelled"


def test_invalid_remote_offer_does_not_starve_other_transfers(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    bad_id = str(uuid4())
    transport.write("synthetic", f"tasks/{bad_id}/offer.json", b"{broken")
    local.tick()
    assert local.journal.task(task_id)["state"] == "receiving"
    assert len(local.journal.tasks()) == 1


def test_oversized_envelope_rejected_before_transport():
    key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError, match="大小限制"):
        wire.envelope(key, "offer", "task", wire.peer_id(wire.public_key(key)), {"huge": "x" * wire.MAX_CONTROL_BYTES})


def test_receiver_only_acknowledges_valid_committed_blocks_after_durable_write(tmp_path):
    local, target, _, transport, remote_key, task_id, item = incoming_receiver(tmp_path, has_video=False)
    study_path = f"tasks/{task_id}/studies/{item['id']}"
    transport.write("synthetic", study_path + ".ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    stem = f"tasks/{task_id}/chunks/{item['id']}.000000"
    transport.write("synthetic", stem + ".bin", b"corrupt bytes")
    ready = {"id": item["id"], **item["blocks"][0]}
    transport.write("synthetic", stem + ".ready.json", wire.envelope(remote_key, "block", task_id, local.peer_id, ready))
    local.tick()
    assert not transport._path(stem + ".bin").exists()
    assert not transport._path(stem + ".ready.json").exists()
    assert local.journal.task(task_id)["acks"] == []
    transport.write("synthetic", stem + ".bin", b"synthetic video bytes")
    local.tick()
    assert local.journal.task(task_id)["acks"] == []
    transport.write("synthetic", stem + ".ready.json", wire.envelope(remote_key, "block", task_id, local.peer_id, ready, sequence=1))
    local.tick()
    assert local.journal.task(task_id)["acks"] == [{"id": item["id"], "index": 0}]
    stage = local.journal.directory / "tasks" / task_id / f"{item['id']}.mp4"
    assert stage.read_bytes() == b"synthetic video bytes"
    assert not target.list_videos()


def test_receiver_reuses_verified_source_hash_before_copying_to_library(tmp_path, monkeypatch):
    import classcue.video_pipeline as video_pipeline

    local, target, _, transport, remote_key, task_id, item = incoming_receiver(tmp_path, has_video=False)
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 10000)
    study_path = f"tasks/{task_id}/studies/{item['id']}"
    transport.write("synthetic", study_path + ".ready.json", wire.envelope(
        remote_key, "study", task_id, local.peer_id,
        {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]},
    ))
    stem = f"tasks/{task_id}/chunks/{item['id']}.000000"
    transport.write("synthetic", stem + ".bin", b"synthetic video bytes")
    transport.write("synthetic", stem + ".ready.json", wire.envelope(
        remote_key, "block", task_id, local.peer_id, {"id": item["id"], **item["blocks"][0]},
    ))
    local.tick()
    original_hash = target.file_hash
    paths = []

    def file_hash(path):
        paths.append(Path(path))
        return original_hash(path)

    monkeypatch.setattr(target, "file_hash", file_hash)
    local.tick()
    assert len(target.list_videos()) == 1
    # Study import independently verifies the final video across its commit boundary.
    assert [path.suffix for path in paths] == [".mp4", ".part", ".mp4"]
    assert len(target.get_segments(target.list_videos()[0]["id"])) == 1
    assert local.journal.task(task_id)["results"][0]["state"] == "imported"


def test_terminal_cleanup_only_removes_expected_payloads_within_own_task(tmp_path):
    local, _, _, transport, _, task_id, item = incoming_receiver(tmp_path)
    other = str(uuid4())
    transport.write("synthetic", f"tasks/{other}/chunks/keep.bin", b"unrelated")
    transport.write("synthetic", f"tasks/{task_id}/chunks/unrecognized.bin", b"unrelated")
    stem = f"tasks/{task_id}/chunks/{item['id']}.000000"
    transport.write("synthetic", stem + ".bin", b"uncommitted")
    transport.write("synthetic", stem + ".ready.json", b"uncommitted")
    local.control(task_id, "cancel")
    local.tick()
    assert not transport._path(stem + ".bin").exists()
    assert not transport._path(stem + ".ready.json").exists()
    assert transport._path(f"tasks/{other}/chunks/keep.bin").read_bytes() == b"unrelated"
    assert transport._path(f"tasks/{task_id}/chunks/unrecognized.bin").read_bytes() == b"unrelated"
    assert transport._path(f"tasks/{task_id}/receipt.json").exists()


def test_changed_after_acceptance_becomes_failed_item_without_overwriting_new_edits(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    target.mark_transcribed(target_id, [TranscriptSegment(2, 0, 1000, "New local work")], "local")
    transport.write("synthetic", f"tasks/{task_id}/studies/{item['id']}.ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    local.tick()
    assert local.journal.task(task_id)["results"][0]["state"] == "failed"
    assert target.get_segments(target_id)[0].text == "New local work"
    local.tick()
    receipt, _ = wire.open_envelope(transport.read("synthetic", f"tasks/{task_id}/receipt.json"))
    assert receipt["data"]["state"] == "partial"


def test_send_retry_creates_new_confirmation_and_preserves_original_failed_task(tmp_path):
    store, video_id = synthetic_store(tmp_path / "library", studied=True)
    transport = FolderTransport(tmp_path / "usb")
    remote_key = ec.generate_private_key(ec.SECP256R1())
    remote_public = wire.public_key(remote_key)
    remote_id = wire.peer_id(remote_public)
    transport.write("synthetic", "identity.json", json.dumps({
        "peer_id": remote_id, "public_key": remote_public, "name": "Synthetic Android", "foreground": True,
    }).encode())
    local = TransferManager(lambda: store, tmp_path / "journal", lambda: tmp_path / "videos",
                            lambda: tmp_path / "audio", transport=transport,
                            identity=ec.generate_private_key(ec.SECP256R1()))
    local.journal.trust(remote_id, "Synthetic Android", remote_public)
    local.scan()
    task_id = local.create_send("synthetic", video_ids=[video_id])["id"]
    item = local.journal.task(task_id)["offer"]["items"][0]
    receipt = {"state": "partial", "items": [{"id": item["id"], "state": "failed", "video_id": None,
                "subtitle_count": 0, "marker_count": 0, "message": "Changed after confirmation"}], "completed_at": wire.now()}
    transport.write("synthetic", f"tasks/{task_id}/receipt.json",
                    wire.envelope(remote_key, "receipt", task_id, local.peer_id, receipt))
    local.tick()
    assert local.journal.task(task_id)["state"] == "completed"
    retry = local.retry_failed(task_id)
    assert retry["id"] != task_id
    assert retry["state"] == "awaiting_confirmation"
    assert local.journal.task(task_id)["results"][0]["state"] == "failed"
    assert local.journal.task(retry["id"])["offer"]["items"][0]["id"] == item["id"]


def test_pairing_response_must_match_both_request_id_and_nonce(tmp_path):
    local, _, _, transport, remote_key, _, _ = incoming_receiver(tmp_path)
    with local.journal.connect() as connection:
        connection.execute("DELETE FROM peers")
    requested = local.request_pairing("synthetic")
    pair = local.journal.pair(requested["id"])
    response = {"request_id": pair["id"], "accepted": True, "name": "Synthetic Android", "nonce": "another-request"}
    path = f"pairing/{pair['id']}.response.json"
    transport.write("synthetic", path, wire.envelope(remote_key, "pair_response", pair["id"], local.peer_id, response))
    local.confirm_pairing(pair["id"])
    assert local.journal.peer(pair["peer_id"]) is None
    assert local.journal.pair(pair["id"])["accepted"] is False
    response["nonce"] = pair["nonce"]
    transport.write("synthetic", path, wire.envelope(remote_key, "pair_response", pair["id"], local.peer_id, response))
    local._pair_responses()
    assert local.journal.peer(pair["peer_id"]) is not None


def test_explicit_pair_rejection_allows_a_fresh_request_and_nonce(tmp_path):
    local, _, _, transport, remote_key, _, _ = incoming_receiver(tmp_path)
    with local.journal.connect() as connection:
        connection.execute("DELETE FROM peers")
    requested = local.request_pairing("synthetic")
    pair = local.journal.pair(requested["id"])
    response = {"request_id": pair["id"], "accepted": False, "name": "Synthetic Android", "nonce": pair["nonce"]}
    transport.write("synthetic", f"pairing/{pair['id']}.response.json",
                    wire.envelope(remote_key, "pair_response", pair["id"], local.peer_id, response))
    local._pair_responses()
    assert local.journal.pair(pair["id"])["rejected"] is True
    retry = local.request_pairing("synthetic")
    assert retry["id"] != pair["id"]
    assert local.journal.pair(retry["id"])["nonce"] != pair["nonce"]


def test_library_change_during_media_read_is_rechecked_before_library_mutation(tmp_path):
    local, target, target_id, transport, remote_key, task_id, item = incoming_receiver(tmp_path)
    switched = VideoStore(tmp_path / "another-library" / "library.sqlite")
    transport.write("synthetic", f"tasks/{task_id}/studies/{item['id']}.ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    original_read = local._read

    def switch_after_read(device, path, maximum=wire.MAX_CONTROL_BYTES):
        content = original_read(device, path, maximum)
        if path.endswith(".studyshare"):
            local.get_store = lambda: switched
        return content

    local._read = switch_after_read
    local.tick()
    assert not switched.list_videos()
    assert not target.get_segments(target_id)
    assert local.journal.task(task_id)["results"][0]["state"] == "failed"


def test_failed_study_import_removes_only_this_tasks_new_uncommitted_video(tmp_path, monkeypatch):
    local, target, _, transport, remote_key, task_id, item = incoming_receiver(tmp_path, has_video=False)
    transport.write("synthetic", f"tasks/{task_id}/studies/{item['id']}.ready.json",
                    wire.envelope(remote_key, "study", task_id, local.peer_id,
                                  {"id": item["id"], "size": item["study"]["size"], "sha256": item["study"]["sha256"]}))
    stem = f"tasks/{task_id}/chunks/{item['id']}.000000"
    transport.write("synthetic", stem + ".bin", b"synthetic video bytes")
    transport.write("synthetic", stem + ".ready.json",
                    wire.envelope(remote_key, "block", task_id, local.peer_id, {"id": item["id"], **item["blocks"][0]}))
    local.tick()
    destination = tmp_path / "videos" / f"{item['id']}.mp4"

    def register(store, stage, audio, video_root, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stage, destination)
        vid = store.create_video(item["id"], destination.name, str(destination), "", 10000,
                                 source_size=destination.stat().st_size, source_mtime_ns=destination.stat().st_mtime_ns)
        return vid, destination, False, None

    def fail(*args, **kwargs):
        raise ValueError("Synthetic study import failure")

    monkeypatch.setattr(transfer_module, "register_managed_video", register)
    monkeypatch.setattr(transfer_module, "import_study", fail)
    local.tick()
    assert not target.list_videos()
    assert not destination.exists()
    assert (tmp_path / "source" / "synthetic.mp4").exists()
    assert local.journal.task(task_id)["results"][0]["state"] == "failed"
