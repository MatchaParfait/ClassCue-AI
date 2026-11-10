from copy import deepcopy
import json
from uuid import uuid4

import pytest

from classcue import device_protocol as wire, study_share
from classcue.study_sync import StudySyncStore, digest, merge, validate, validate_request, anchor
from classcue.device_study import current_state_digest, import_study
from tests.test_device_transfers import synthetic_store, incoming_receiver


def baseline(tmp_path):
    library, video = synthetic_store(tmp_path, studied=True)
    store = StudySyncStore(library)
    request = store.request("tablet", video)
    return library, video, store, request["document"]


def test_canonical_shared_vector_and_strict_validation(tmp_path):
    _, _, _, document = baseline(tmp_path)
    assert wire.canonical({"text": "<>&\u2028\u2029", "number": 1e-7}) == b'{"number":0.0000001,"text":"<>&\\u2028\\u2029"}'
    assert digest({"id": 1, "start_ms": 0, "end_ms": 1000, "text": "Synthetic subtitle"}) == "efbf896bd16cba4adf92527642b1f1f8844f71b3c6911d13bd73ab189c4bc2f1"
    for key, bad in (("removed", 1), ("confidence", True), ("title", " "), ("start_ms", 0.0), ("id", "bad")):
        invalid = deepcopy(document)
        invalid["records"][0][key] = bad
        with pytest.raises(ValueError):
            validate(invalid)
    with pytest.raises(ValueError):
        validate_request({"bootstrap": True, "base_digest": "a" * 64, "document": document})


def test_different_fields_merge_and_same_field_requires_choice(tmp_path):
    _, _, _, base = baseline(tmp_path)
    local, remote = deepcopy(base), deepcopy(base)
    local["records"][0]["note"] = "PC note"
    remote["records"][0]["title"] = "Tablet title"
    merged, conflicts = merge(base, local, remote)
    assert not conflicts
    assert merged["records"][0]["note"] == "PC note"
    assert merged["records"][0]["title"] == "Tablet title"
    local["records"][0]["title"] = "PC title"
    merged, conflicts = merge(base, local, remote)
    identity = local["records"][0]["id"]
    assert conflicts[0]["fields"] == ["title"]
    merged, conflicts = merge(base, local, remote, {identity: "remote"})
    assert not conflicts
    assert merged["records"][0]["title"] == "Tablet title"
    assert merged["records"][0]["note"] == "PC note"


def test_delete_edit_conflict_and_missing_record_is_never_delete(tmp_path):
    _, _, _, base = baseline(tmp_path)
    local, remote = deepcopy(base), deepcopy(base)
    local["records"][0]["removed"] = True
    remote["records"][0]["note"] = "new note"
    assert merge(base, local, remote)[1][0]["fields"] == ["removed"]
    remote["records"] = []
    with pytest.raises(ValueError, match="缺失"):
        merge(base, local, remote)


def test_bootstrap_receipt_persists_ids_and_preserves_new_edits(tmp_path):
    library, video, store, sent = baseline(tmp_path)
    request = {"bootstrap": True, "base_digest": None, "document": sent}
    remote = deepcopy(sent)
    remote["records"][0]["title"] = "Tablet corrected title"
    identity = sent["records"][0]["id"]
    marker = library.list_markers(video)[0]["id"]
    library.set_marker_importance(marker, "critical")
    plan = store.plan("tablet", request, remote)
    store.accept("tablet", "request-1", request, digest(plan["local"]), {}, remote)
    result = store.export(video)
    assert result["records"][0]["title"] == "Tablet corrected title"
    assert result["records"][0]["importance"] == "critical"
    assert result["records"][0]["id"] == identity
    assert library.list_markers(video)[0]["id"] == marker
    assert store.request("tablet", video)["base_digest"] == digest(remote)
    assert store.accept("tablet", "request-1", request, "unused", {}, remote) == remote
    with pytest.raises(ValueError, match="复用"):
        store.accept("tablet", "request-1", {**request, "bootstrap": False}, "unused", {}, remote)


def link(store, video, document):
    request = {"bootstrap": True, "base_digest": None, "document": document}
    store.accept("tablet", str(uuid4()), request, digest(document), {}, document)


def test_incoming_added_question_tombstone_restore_and_replay(tmp_path):
    library, video, store, base = baseline(tmp_path)
    link(store, video, base)
    remote = deepcopy(base)
    remote["records"][0]["removed"] = True
    question = {**remote["records"][0], "id": str(uuid4()), "mode": "question_extraction",
                "kind": "question", "origin": "manual", "trigger_phrase": "MANUAL-question:synthetic",
                "scheme_name": "考题", "title": "New question", "removed": False}
    question["anchor"] = anchor(question)
    remote["records"].append(question)
    remote["records"].sort(key=lambda r: r["id"])
    request = {"bootstrap": False, "base_digest": digest(base), "document": remote}
    plan = store.plan("tablet", request)
    receipt = store.accept("tablet", "incoming", request, digest(plan["local"]), {})
    assert store.export(video) == receipt
    assert len(library.list_markers(video, "question_extraction")) == 1
    assert len(library.list_excluded_markers(video)) == 1
    assert store.accept("tablet", "incoming", request, "unused", {}) == receipt
    assert len(library.list_markers(video)) == 1
    library.set_marker_review_status(library.list_excluded_markers(video)[0]["id"], "active")
    assert not any(row["removed"] for row in store.request("tablet", video)["document"]["records"])


def test_stale_preview_rolls_back_and_preserves_baseline(tmp_path):
    library, video, store, base = baseline(tmp_path)
    link(store, video, base)
    request = store.request("tablet", video)
    expected = digest(base)
    library.set_marker_importance(library.list_markers(video)[0]["id"], "important")
    with pytest.raises(ValueError, match="改变"):
        store.accept("tablet", "stale", request, expected, {})
    assert store.request("tablet", video)["base_digest"] == digest(base)
    with library.connect() as connection:
        assert store.replay("tablet", "stale", request, connection) is None


def test_failure_after_marker_writes_rolls_back_baseline_receipt_and_records(tmp_path, monkeypatch):
    _, video, store, base = baseline(tmp_path)
    link(store, video, base)
    request = store.request("tablet", video)
    request["document"]["records"][0]["title"] = "Incoming change"
    original = store._apply

    def fail_after_apply(*args):
        original(*args)
        raise ValueError("Synthetic failure after marker writes")

    monkeypatch.setattr(store, "_apply", fail_after_apply)
    with pytest.raises(ValueError, match="Synthetic"):
        store.accept("tablet", "rolled-back", request, digest(base), {})
    assert store.export(video) == base
    assert store.request("tablet", video)["base_digest"] == digest(base)
    with store.library.connect() as connection:
        assert store.replay("tablet", "rolled-back", request, connection) is None


def test_learning_edit_does_not_claim_that_failed_analysis_completed(tmp_path):
    library, video, store, base = baseline(tmp_path)
    link(store, video, base)
    library.set_status(video, "failed")
    request = store.request("tablet", video)
    request["document"]["records"][0]["note"] = "Tablet correction"
    store.accept("tablet", "correction", request, digest(base), {})
    assert library.get_video(video)["status"] == "failed"


def test_manual_question_sync_does_not_claim_ai_extraction_completed(tmp_path):
    library, video, store, base = baseline(tmp_path)
    link(store, video, base)
    remote = deepcopy(base)
    question = {**base["records"][0], "id": str(uuid4()), "mode": "question_extraction",
                "kind": "question", "origin": "manual", "trigger_phrase": "MANUAL-question:synthetic",
                "scheme_name": "考题", "scheme_revision": 0}
    question["anchor"] = anchor(question)
    remote["records"].append(question)
    request = {"bootstrap": False, "base_digest": digest(base), "document": remote}
    store.accept("tablet", "manual-only", request, digest(base), {})
    assert len(library.list_markers(video, "question_extraction")) == 1
    assert library.get_analysis_run(video, "question_extraction") is None


def test_builtin_scheme_metadata_merges_independent_questions_and_legacy_labels(tmp_path):
    library, video, store, base = baseline(tmp_path)
    library.create_manual_question(video, 500)
    library.set_analysis_run(video, "question_extraction", "completed")
    with library.connect() as connection:
        connection.execute("UPDATE video_analysis_runs SET scheme_revision=7 WHERE video_id=? AND analysis_mode=?",
                           (video, "question_extraction"))
    local = store.export(video)
    remote = deepcopy(base)
    question = deepcopy(next(row for row in local["records"] if row["kind"] == "question"))
    question.update(id=str(uuid4()), scheme_name="考题", scheme_revision=3)
    remote["records"].append(question)
    for row in remote["records"]:
        if row["kind"] == "point":
            row.update(scheme_name="旧显示名称", scheme_revision=9)
    merged, conflicts = merge(base, local, remote)
    assert not conflicts
    questions = [row for row in merged["records"] if row["kind"] == "question"]
    assert len(questions) == 2
    assert {(row["scheme_name"], row["scheme_revision"]) for row in questions} == {("视频考题", 0)}
    assert library.get_analysis_run(video, "question_extraction")["scheme_revision"] == 7
    assert next(row for row in local["records"] if row["kind"] == "question")["scheme_revision"] == 0


@pytest.mark.parametrize("removed", [False, True])
def test_reanalysis_retains_synced_title_edits_and_tombstones(tmp_path, removed):
    library, video, store, _ = baseline(tmp_path)
    result = {"start_ms": 100, "end_ms": 200, "title": "Original AI title",
              "reason": "Original AI note", "source_quote": "Synthetic", "signal_type": "exam"}
    library.publish_exam_results(video, "strict_exam_intent", [result], "first", "first")
    before = store.export(video)
    marker = library.list_markers(video, "strict_exam_intent")[0]
    with library.connect() as connection:
        connection.execute("UPDATE video_markers SET title=?,note=?,review_status=? WHERE id=?",
                           ("Tablet corrected title", "Tablet note", "excluded" if removed else "active", marker["id"]))
    changed = store.export(video)
    library.publish_exam_results(video, "strict_exam_intent", [result], "second", "second")
    assert store.export(video) == changed
    assert len(store.export(video)["records"]) == len(before["records"])
    with library.connect() as connection:
        identity = next(row["id"] for row in changed["records"] if row["mode"] == "strict_exam_intent")
        assert connection.execute("SELECT id FROM video_markers WHERE sync_id=?", (identity,)).fetchone()[0] == marker["id"]


def test_pending_bootstrap_prevents_destructive_legacy_reset(tmp_path):
    library, video, store, base = baseline(tmp_path)
    store.save({"id": str(uuid4()), "video_id": video, "state": "waiting_peer"})
    with pytest.raises(ValueError, match="关联"):
        library.replace_transcript_for_reanalysis(video, [], "synthetic")
    assert store.export(video) == base


@pytest.mark.parametrize("change", ["question", "removed"])
def test_legacy_overwrite_rejects_edits_omitted_from_share_v1(tmp_path, change):
    from classcue.device_study import current_digest
    from classcue.device_transfer_store import TransferStore
    library, video, store, _ = baseline(tmp_path)
    incoming = json.loads(study_share.export_package(library, video))
    incoming["markers"][0]["title"] = "Incoming legacy point"
    marker = library.list_markers(video)[0]
    if change == "removed":
        library.set_marker_review_status(marker["id"], "excluded")
    before = current_state_digest(library, video)
    semantic = current_digest(library, video)
    if change == "question":
        library.create_manual_question(video, 500)
    else:
        with library.connect() as connection:
            connection.execute("UPDATE video_markers SET note='New removed note' WHERE id=?", (marker["id"],))
    assert current_digest(library, video) == semantic
    edited = store.export(video)
    with pytest.raises(ValueError, match="接收确认后资料已改变"):
        import_study(library, TransferStore(tmp_path / "journal"), video, json.dumps(incoming).encode(),
                     overwrite=True, expected_digest=before)
    assert store.export(video) == edited


def test_legacy_confirmation_requires_reconfirmation_after_new_question(tmp_path):
    manager, library, video, _, _, identity, item = incoming_receiver(tmp_path)
    from classcue.core import TranscriptSegment
    library.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle")])
    library.add_marker(video, 0, 1000, "Local point", "", "Synthetic subtitle", "Important", .9, "medium_recall")
    library.set_analysis_run(video, "medium_recall", "completed", 1)
    library.set_status(video, "review_ready")
    task = manager.journal.task(identity)
    task.update(state="awaiting_confirmation", preview=manager._preview(task["offer"]))
    manager.journal.save(task)
    question = library.create_manual_question(video, 500)
    with pytest.raises(ValueError, match="核对更新后的预览"):
        manager.accept(identity, [{"id": item["id"], "action": "overwrite"}])
    assert manager.journal.task(identity)["state"] == "awaiting_confirmation"
    assert library.list_markers(video, "question_extraction")[0]["id"] == question["id"]
    assert manager.accept(identity, [{"id": item["id"], "action": "overwrite"}])["state"] == "receiving"


def test_equivalent_legacy_reuse_preserves_questions_even_with_an_old_preview(tmp_path):
    from classcue.device_transfer_store import TransferStore
    library, video, _, _ = baseline(tmp_path)
    content = study_share.export_package(library, video)
    before = current_state_digest(library, video)
    question = library.create_manual_question(video, 500)
    assert import_study(library, TransferStore(tmp_path / "journal"), video, content,
                        overwrite=True, expected_digest=before) is None
    assert library.list_markers(video, "question_extraction")[0]["id"] == question["id"]


def test_reanalysis_and_legacy_overwrite_preserve_sync(tmp_path):
    library, video, store, base = baseline(tmp_path)
    content = study_share.export_package(library, video)
    link(store, video, base)
    library.delete_markers_for_mode(video, "medium_recall")
    assert store.export(video) == base
    library.publish_semantic_markers(video, "medium_recall", [], replace_existing=True)
    assert store.export(video) == base
    with pytest.raises(ValueError, match="关联"):
        library.replace_transcript_for_reanalysis(video, [], "synthetic")
    changed = json.loads(content)
    changed["markers"][0]["title"] = "Legacy replacement"
    from classcue.device_transfer_store import TransferStore
    with pytest.raises(ValueError, match="关联"):
        import_study(library, TransferStore(tmp_path / "journal"), video, json.dumps(changed).encode(),
                     overwrite=True, expected_digest=current_state_digest(library, video))
    library.delete_marker(library.list_markers(video)[0]["id"])
    assert store.export(video)["records"][0]["removed"]


def test_transfer_projects_questions_without_changing_share_v1(tmp_path):
    library, video, store, _ = baseline(tmp_path)
    library.create_manual_question(video, 500)
    library.set_analysis_run(video, "question_extraction", "completed", 1)
    content = study_share.export_package(library, video, transfer=True)
    assert len(study_share.decode_package(content).markers) == 1
    assert {row["kind"] for row in store.export(video)["records"]} == {"point", "question"}


def test_signed_usb_journal_retry_and_ack_without_duplicate_records(tmp_path):
    manager, library, video, transport, key, _, _ = incoming_receiver(tmp_path)
    # Establish a synthetic shared baseline using the actual Windows store.
    store = manager.sync.store()
    base = store.export(video) if library.get_segments(video) else None
    if base is None:
        from classcue.core import TranscriptSegment
        library.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle")])
        library.add_marker(video, 0, 1000, "Synthetic marker", "Note", "Synthetic subtitle", "Important", .9, "medium_recall")
        library.set_status(video, "review_ready")
        base = store.export(video)
    peer = wire.peer_id(wire.public_key(key))
    initial = {"bootstrap": True, "base_digest": None, "document": base}
    store.accept(peer, "bootstrap", initial, digest(base), {}, base)
    request = store.request(peer, video)
    request["document"]["records"][0]["title"] = "Offline tablet correction"
    identity = str(uuid4())
    transport.write("synthetic", f"sync/{identity}/request.json",
                    wire.envelope(key, "sync_request", identity, manager.peer_id, request))
    manager.tick()
    assert store.task(identity)["state"] == "awaiting_confirmation"
    manager.sync.accept(identity, {})
    manager.tick()
    receipt, _ = wire.open_envelope(transport.read("synthetic", f"sync/{identity}/receipt.json"))
    assert library.list_markers(video)[0]["title"] == "Offline tablet correction"
    # Recreate a pre-journal state after a durable commit.
    task = store.task(identity)
    task["state"] = "awaiting_confirmation"
    task.pop("receipt")
    task["published_at"] = 0
    store.save(task)
    manager.tick()
    assert store.task(identity)["state"] == "awaiting_ack"
    ack = {"request_digest": digest(request), "document_digest": digest(receipt["data"]["document"])}
    transport.write("synthetic", f"sync/{identity}/ack.json", wire.envelope(key, "sync_ack", identity, manager.peer_id, ack))
    manager.tick()
    assert store.task(identity)["state"] == "completed"
    assert len(library.list_markers(video)) == 1


def test_simultaneous_requests_yield_to_the_same_uuid_without_losing_edits(tmp_path):
    manager, library, video, transport, key, _, _ = incoming_receiver(tmp_path)
    from classcue.core import TranscriptSegment
    library.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle")])
    library.add_marker(video, 0, 1000, "Synthetic marker", "Note", "Synthetic subtitle", "Important", .9, "medium_recall")
    library.set_status(video, "review_ready")
    peer = wire.peer_id(wire.public_key(key))
    identity = json.loads(transport.read("synthetic", "identity.json"))
    identity["capabilities"] = ["study-sync-v1"]
    transport.write("synthetic", "identity.json", json.dumps(identity).encode())
    manager.scan()
    store = manager.sync.store()
    base = store.export(video)
    request = {"bootstrap": True, "base_digest": None, "document": base}
    store.accept(peer, "initial", request, digest(base), {}, base)
    library.set_marker_importance(library.list_markers(video)[0]["id"], "critical")
    outgoing = manager.sync.start("synthetic", [video])[0]
    remote = {"bootstrap": False, "base_digest": digest(base), "document": deepcopy(base)}
    remote["document"]["records"][0]["note"] = "Offline note"
    winner = "00000000-0000-0000-0000-000000000001"
    transport.write("synthetic", f"sync/{winner}/request.json", wire.envelope(key, "sync_request", winner, manager.peer_id, remote))
    manager.tick()
    assert store.task(outgoing["id"])["state"] == "rejected"
    assert store.task(winner)["state"] == "awaiting_confirmation"
    manager.sync.accept(winner, {})
    result = store.export(video)["records"][0]
    assert result["importance"] == "critical" and result["note"] == "Offline note"


def test_new_request_is_discovered_after_a_thousand_known_directories(tmp_path, monkeypatch):
    manager, library, video, transport, key, _, _ = incoming_receiver(tmp_path)
    from classcue.core import TranscriptSegment
    library.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "Synthetic subtitle")])
    library.add_marker(video, 0, 1000, "Synthetic marker", "", "Synthetic subtitle", "Important", .9, "medium_recall")
    peer = wire.peer_id(wire.public_key(key))
    store = manager.sync.store()
    base = store.export(video)
    initial = {"bootstrap": True, "base_digest": None, "document": base}
    store.accept(peer, "initial", initial, digest(base), {}, base)
    known = [{"id": str(uuid4()), "peer_id": peer, "video_id": video, "state": "completed"} for _ in range(1000)]
    identity = str(uuid4())
    request = store.request(peer, video)
    transport.write("synthetic", f"sync/{identity}/request.json",
                    wire.envelope(key, "sync_request", identity, manager.peer_id, request))
    original_list = transport.list
    monkeypatch.setattr(transport, "list", lambda device, path:
                        [{"name": task["id"], "folder": True} for task in known] + [{"name": identity, "folder": True}]
                        if path == "sync" else original_list(device, path))
    monkeypatch.setattr(store, "tasks", lambda: known)
    device = manager._device("synthetic")
    manager.sync._discover(store, device)
    assert store.task(identity)["state"] == "awaiting_confirmation"
