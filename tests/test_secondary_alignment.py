from dataclasses import asdict
import json
from pathlib import Path
from threading import Event
import time
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.core import TranscriptSegment
from classcue.secondary_alignment import Matcher, anchors, search_ranges
from classcue.secondary_alignment_worker import run_alignment
from classcue.video_store import VideoStore
import api.secondary_alignment as alignment_api
import classcue.secondary_alignment_service as service

FIRST = "今天我们学习函数的连续性以及对应的计算方法"
SECOND = "接下来讨论导数的定义并且完成一道相关的例题"


def segment(index, start, text, *, words=False, end=None):
    finish = end or start + 6000
    tokens = [{"word": char, "start": (start + i * 200) / 1000,
               "end": (start + (i + 1) * 200) / 1000} for i, char in enumerate(text)] if words else None
    return TranscriptSegment(index, start, finish, text, tokens)


def test_search_starts_at_late_subtitles_and_deduplicates_ranges():
    primary = [segment(1, 1200000, FIRST), segment(2, 1230000, SECOND)]
    assert anchors(primary)[0].start_ms >= 1200000
    ranges = list(search_ranges(primary, 8000, 2000000))
    assert ranges[0][0] >= 1193
    for i, (start, end) in enumerate(ranges):
        assert start < end
        assert all(max(start, a) >= min(end, b) for a, b in ranges[:i])


@pytest.mark.parametrize("offset", [8000, -8000])
def test_noise_is_skipped_and_independent_matching_sentences_confirm_offset(offset):
    primary = [segment(1, 20000, FIRST), segment(2, 50000, SECOND)]
    secondary = [segment(1, 2000, "完全不相干的噪声生成了主视频里不存在的一段文字"),
                 segment(2, 20000 + offset, FIRST), segment(3, 50000 + offset, SECOND)]
    result = Matcher(primary).evaluate(secondary, {1: 1, 2: 2, 3: 3})
    assert result["outcome"] == "ready"
    assert result["offset_ms"] == offset
    assert len(result["evidence"]) == 2


def test_repeated_main_sentence_and_shared_short_words_are_not_matches():
    matcher = Matcher([segment(1, 20000, FIRST), segment(2, 100000, FIRST)])
    assert matcher.evaluate([segment(1, 28000, FIRST)], {1: 1})["outcome"] == "no_match"
    assert matcher.evaluate([segment(1, 28000, "这个然后对不对")], {1: 1})["outcome"] == "no_match"
    repeated = Matcher([segment(1, 20000, FIRST + FIRST)])
    assert repeated.evaluate([segment(1, 28000, FIRST)], {1: 1})["outcome"] == "no_match"


def test_two_fragments_of_same_window_do_not_automatically_apply():
    primary = [segment(1, 20000, FIRST), segment(2, 50000, SECOND)]
    secondary = [segment(1, 28000, FIRST), segment(2, 58000, SECOND)]
    assert Matcher(primary).evaluate(secondary, {1: 1, 2: 1})["outcome"] == "candidate"


def test_conflicting_offsets_are_not_averaged():
    primary = [segment(1, 20000, FIRST), segment(2, 50000, SECOND)]
    secondary = [segment(1, 28000, FIRST), segment(2, 68000, SECOND)]
    assert Matcher(primary).evaluate(secondary, {1: 1, 2: 2})["outcome"] == "conflict"


def test_word_timestamps_ignore_hallucinated_prefix_time():
    primary = [segment(1, 20000, FIRST, words=True)]
    secondary = [segment(1, 28000, "噪声" + FIRST, words=True)]
    result = Matcher(primary).evaluate(secondary, {1: 1})
    assert result["outcome"] == "candidate"
    assert result["offset_ms"] == 8400


def test_long_hallucination_with_small_true_excerpt_is_rejected():
    text = "完全无关的录音背景噪声" * 5 + FIRST
    assert Matcher([segment(1, 20000, FIRST)]).evaluate([segment(1, 28000, text)], {1: 1})["outcome"] == "no_match"


def test_raw_noise_uses_the_ten_segment_budget_and_temp_audio_is_removed(tmp_path):
    calls = []
    def recognize(path):
        calls.append(path)
        return [segment(i, 0, "无法匹配的不同噪声内容") for i in range(15)]
    result = run_alignment(
        {"segments": [asdict(segment(1, 1200000, FIRST))], "model": {"id": "synthetic"},
         "secondary_path": "synthetic.mp4", "duration_ms": 2000000, "original_offset_ms": 0},
        tmp_path, recognize=recognize,
        read_audio=lambda *args, **kwargs: (np.ones(30 * 16000, dtype=np.float32), 30, False),
        detect=lambda audio: [{"start": 0, "end": len(audio)}],
    )
    assert result["segments_checked"] == 10
    assert result["asr_calls"] == len(calls) == 1
    assert result["outcome"] == "no_match"
    assert not list(tmp_path.glob("*.wav"))


def test_silence_never_calls_asr_and_decode_budget_is_finite(tmp_path):
    scans = []
    def read(*args, **kwargs):
        scans.append(args[1:3])
        return np.zeros(30 * 16000, dtype=np.float32), 30, False
    result = run_alignment(
        {"segments": [asdict(segment(i, 1200000 + i * 60000, FIRST)) for i in range(3)],
         "model": {"id": "synthetic"}, "secondary_path": "synthetic.mp4",
         "duration_ms": 2000000, "original_offset_ms": 0},
        tmp_path, recognize=lambda _: pytest.fail("ASR called on silence"),
        read_audio=read, detect=lambda _: [],
    )
    assert result["asr_calls"] == 0
    assert result["decoded_seconds"] <= 600
    assert scans[0][0] > 1100


def test_empty_asr_outputs_still_consume_call_and_audio_budget(tmp_path):
    result = run_alignment(
        {"segments": [asdict(segment(1, 1200000, FIRST))], "model": {"id": "synthetic"},
         "secondary_path": "synthetic.mp4", "duration_ms": 2000000, "original_offset_ms": 0},
        tmp_path, recognize=lambda _: [],
        read_audio=lambda *args, **kwargs: (np.ones(30 * 16000, dtype=np.float32), 30, False),
        detect=lambda audio: [{"start": 0, "end": len(audio)}],
    )
    assert result["asr_calls"] <= 10
    assert result["asr_seconds"] <= 120
    assert result["segments_checked"] == 0
    assert result["outcome"] == "no_match"


def test_window_timestamps_produce_the_correct_offset_end_to_end(tmp_path):
    first = FIRST + "我们需要认真理解这个重要结论"
    primary = [segment(1, 20000, first), segment(2, 50000, SECOND)]
    outputs = iter([[segment(1, 0, first)], [segment(1, 0, SECOND)]])
    result = run_alignment(
        {"segments": [asdict(s) for s in primary], "model": {"id": "synthetic"},
         "secondary_path": "synthetic.mp4", "duration_ms": 200000, "original_offset_ms": 8000},
        tmp_path, recognize=lambda _: next(outputs),
        read_audio=lambda *args, **kwargs: (np.zeros(30 * 16000, dtype=np.float32), 30, False),
        detect=lambda _: [{"start": 15 * 16000, "end": 27 * 16000}],
    )
    assert result["outcome"] == "ready"
    assert result["offset_ms"] == 8000
    assert result["asr_calls"] == 2
    assert result["segments_checked"] == 2


def test_local_decode_seeks_to_media_time_and_preserves_silence(tmp_path):
    from classcue.media import read_audio_window
    source = tmp_path / "synthetic.wav"
    audio = np.zeros(16000 * 8, dtype="<i2")
    audio[16000 * 5:16000 * 6] = 10000
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(audio.tobytes())
    samples, decoded, exhausted = read_audio_window(source, 4, 7, decoded_budget=10)
    assert len(samples) == 48000
    assert np.abs(samples[:15000]).max() == 0
    assert samples[17000:30000].mean() > .2
    assert samples[33000:].max() == 0
    assert decoded < 5
    assert not exhausted


@pytest.fixture
def library(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "project.db")
    ids = []
    for number in range(2):
        path = tmp_path / f"camera-{number}.mp4"
        path.write_bytes(b"synthetic")
        ids.append(store.create_video(f"hash-{number}", path.name, str(path), str(path.with_suffix(".wav")), 120000))
    store.mark_transcribed(ids[0], [segment(1, 20000, FIRST), segment(2, 50000, SECOND)])
    store.set_secondary_view(ids[0], ids[1], 5000)
    monkeypatch.setattr(alignment_api, "require_prepared_asr_model", lambda _: None)
    monkeypatch.setattr(service, "model_selection", lambda: {"id": "whisper-turbo", "device": "cpu"})
    app = create_app(store)
    with TestClient(app) as client:
        yield client, store, ids


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(.02)
    pytest.fail("alignment job did not finish")


def test_auto_apply_result_and_guarded_undo(library, monkeypatch):
    client, store, (primary, secondary) = library
    monkeypatch.setattr(alignment_api, "run_worker", lambda *args: {
        "outcome": "ready", "offset_ms": 8200, "message": "match", "evidence": [],
    })
    endpoint = f"/api/videos/{primary}/secondary-view"
    response = client.post(endpoint + "/alignment")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "completed", job
    assert job["alignment"]["outcome"] == "applied"
    assert client.get(endpoint).json()["alignment_job_id"] == job_id
    assert store.get_secondary_view(primary)["offset_ms"] == 8200
    assert client.post(endpoint + f"/alignment/{job_id}/undo").status_code == 200
    assert store.get_secondary_view(primary)["offset_ms"] == 5000
    assert client.post(endpoint + f"/alignment/{job_id}/undo").status_code == 200
    assert client.post(endpoint + f"/alignment/{job_id}/apply").status_code == 409
    assert store.get_video(primary)["status"] == "transcribed"
    assert not store.get_segments(secondary)


def test_candidate_needs_explicit_apply_and_cannot_overwrite_manual_change(library, monkeypatch):
    client, store, (primary, _) = library
    monkeypatch.setattr(alignment_api, "run_worker", lambda *args: {
        "outcome": "candidate", "offset_ms": -8000, "message": "candidate", "evidence": [],
    })
    endpoint = f"/api/videos/{primary}/secondary-view"
    job_id = client.post(endpoint + "/alignment").json()["job_id"]
    wait_job(client, job_id)
    assert store.get_secondary_view(primary)["offset_ms"] == 5000
    assert client.post(endpoint + f"/alignment/{job_id}/apply").status_code == 200
    assert store.get_secondary_view(primary)["offset_ms"] == -8000
    assert client.post(endpoint + f"/alignment/{job_id}/apply").status_code == 200
    client.patch(endpoint, json={"offset_ms": 1000})
    assert client.post(endpoint + f"/alignment/{job_id}/undo").status_code == 409


def test_setting_changed_during_inference_is_not_overwritten_and_requests_deduplicate(library, monkeypatch):
    client, store, (primary, _) = library
    entered, release = Event(), Event()
    def run(*args):
        entered.set()
        assert release.wait(5)
        return {"outcome": "ready", "offset_ms": 8200, "message": "match", "evidence": []}
    monkeypatch.setattr(alignment_api, "run_worker", run)
    endpoint = f"/api/videos/{primary}/secondary-view"
    try:
        job_id = client.post(endpoint + "/alignment").json()["job_id"]
        assert entered.wait(3)
        assert client.post(endpoint + "/alignment").json()["job_id"] == job_id
        client.patch(endpoint, json={"offset_ms": 3000, "use_primary_audio": True})
    finally:
        release.set()
    assert wait_job(client, job_id)["alignment"]["outcome"] == "stale"
    view = store.get_secondary_view(primary)
    assert view["offset_ms"] == 3000 and view["use_primary_audio"] == 1


def test_cancelled_alignment_keeps_course_status_and_old_offset(library, monkeypatch):
    client, store, (primary, _) = library
    entered = Event()
    def run(_store, _snapshot, _update, cancelled):
        entered.set()
        deadline = time.monotonic() + 4
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.01)
        return {"outcome": "ready", "offset_ms": 8200, "message": "match", "evidence": []}
    monkeypatch.setattr(alignment_api, "run_worker", run)
    endpoint = f"/api/videos/{primary}/secondary-view"
    job_id = client.post(endpoint + "/alignment").json()["job_id"]
    assert entered.wait(3)
    client.post(f"/api/jobs/{job_id}/cancel")
    assert wait_job(client, job_id)["status"] == "interrupted"
    assert store.get_secondary_view(primary)["offset_ms"] == 5000
    assert store.get_video(primary)["status"] == "transcribed"


def test_audio_setting_response_preserves_running_alignment(library, monkeypatch):
    client, store, (primary, _) = library
    entered, release = Event(), Event()

    def run(*args):
        entered.set()
        assert release.wait(5)
        return {"outcome": "ready", "offset_ms": 8200, "message": "match", "evidence": []}

    monkeypatch.setattr(alignment_api, "run_worker", run)
    endpoint = f"/api/videos/{primary}/secondary-view"
    try:
        job_id = client.post(endpoint + "/alignment").json()["job_id"]
        assert entered.wait(3)
        response = client.patch(endpoint, json={"use_primary_audio": True})
        assert response.status_code == 200
        assert response.json()["alignment_job_id"] == job_id
        assert response.json()["use_primary_audio"] == 1
        assert response.json()["offset_ms"] == 5000
    finally:
        release.set()
    job = wait_job(client, job_id)
    assert job["alignment"]["outcome"] == "applied"
    assert store.get_secondary_view(primary)["use_primary_audio"] == 1
    assert store.get_secondary_view(primary)["offset_ms"] == 8200


def test_prepared_only_never_downloads(monkeypatch, tmp_path):
    import classcue.transcriber as transcriber
    from contextlib import nullcontext
    monkeypatch.setattr(transcriber, "model_operation", lambda _: nullcontext())
    monkeypatch.setattr(transcriber, "model_is_downloaded", lambda _: False)
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *args: pytest.fail("download attempted"))
    with pytest.raises(transcriber.ASRSetupError, match="已准备"):
        transcriber.transcribe_audio(tmp_path / "synthetic.wav", model_id="whisper-turbo", prepared_only=True)


def test_owned_worker_scans_synthetic_silence_without_loading_asr(library):
    _client, store, (primary, secondary) = library
    source = Path(store.get_video(secondary)["video_path"])
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(bytes(120 * 16000 * 2))
    snapshot = service.alignment_snapshot(store, primary, "synthetic")
    result = service.run_worker(store, snapshot, lambda *args: None, lambda: False)
    assert result["outcome"] == "no_match"
    assert result["asr_calls"] == 0
    assert not list((store.path.parent / "temporary_audio").iterdir())


def test_restart_restores_an_already_committed_result(library):
    _client, store, (primary, _) = library
    snapshot = service.alignment_snapshot(store, primary, "synthetic")
    store.create_job("committed-alignment", service.JOB_TYPE, primary)
    service.save_result(store, "committed-alignment", snapshot,
                        {"outcome": "ready", "offset_ms": 8000, "message": "match", "evidence": []}, apply=True)
    store.mark_jobs_interrupted()
    assert store.get_job("committed-alignment")["status"] == "completed"
    assert store.get_secondary_view(primary)["offset_ms"] == 8000


def test_alignment_queues_behind_an_existing_job(library, monkeypatch):
    client, store, (primary, _) = library
    entered, release = Event(), Event()
    def blocking(update, cancelled):
        entered.set()
        assert release.wait(5)
    jobs = client.app.state.jobs
    jobs.submit("video_analysis", None, blocking)
    assert entered.wait(3)
    worker = []
    def run(*args):
        worker.append(True)
        return {"outcome": "no_match", "offset_ms": None, "message": "no match", "evidence": []}
    monkeypatch.setattr(alignment_api, "run_worker", run)
    try:
        response = client.post(f"/api/videos/{primary}/secondary-view/alignment")
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert store.get_job(job_id)["status"] == "queued"
        assert not worker
    finally:
        release.set()
    assert wait_job(client, job_id)["alignment"]["outcome"] == "no_match"
