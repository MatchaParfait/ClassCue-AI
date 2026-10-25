"""Legacy SemanticJudge screening must honour cancellation and keep old results.

``strict_exam_intent`` still reaches :func:`create_semantic_markers` through
``analyze_prepared_video_once``/``process_saved_analysis_once`` whenever the judge
has no ``analyze_video`` (the ``OpenAICompatibleJudge`` adapter).  These tests pin
the contract already followed by the production ``ExamPointJudge`` path: no model
calls once a cancel is requested, no partial or empty publication, old markers
plus review decisions retained on cancel or request failure, and one atomic
replace covering the whole mode on success.
"""

import threading
from types import SimpleNamespace

import pytest

import classcue.video_pipeline as video_pipeline
from classcue.core import SemanticCandidate, SemanticSelection, TranscriptSegment
from classcue.processing_control import ProcessingCancelled
from classcue.video_pipeline import (
    analyze_prepared_video_once,
    process_saved_analysis_once,
)
from classcue.video_store import VideoStore


class ScriptedJudge:
    """A legacy judge (no ``analyze_video``) with scripted per-chunk behaviour."""

    def __init__(self, behavior, *, concurrency=1):
        self.behavior = behavior
        self.profile = SimpleNamespace(request_concurrency=concurrency)
        self._lock = threading.Lock()
        self.calls = 0

    def judge_semantic_chunk(self, chunk):
        with self._lock:
            self.calls += 1
            calls = self.calls
        return self.behavior(calls, chunk)


def selection_for(chunk, title):
    return SemanticSelection(candidates=[
        SemanticCandidate(
            level="confirmed",
            start_sentence_id=chunk[0].id,
            end_sentence_id=chunk[-1].id,
            title=title,
            reason="fixture reason",
            evidence_quote=chunk[0].text,
        )
    ])


def make_store(tmp_path, segments):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 300000)
    store.mark_transcribed(video_id, segments)
    return store, video_id


def spaced_segments(count):
    # Ten-second gaps keep successive candidates outside merge_semantic_drafts.
    return [
        TranscriptSegment(index, index * 10000, index * 10000 + 500, f"第 {index} 句")
        for index in range(1, count + 1)
    ]


def single_segment_chunks(monkeypatch):
    monkeypatch.setattr(
        video_pipeline, "build_semantic_chunks", lambda values: [(segment,) for segment in values]
    )


def marker_titles(store, video_id, mode):
    return [marker["title"] for marker in store.list_markers(video_id, mode)]


def test_cancelled_reanalysis_keeps_old_markers_without_model_calls(tmp_path):
    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.add_marker(video_id, 0, 3000, "旧考点", "旧注释", "这个会考。", "AI-confirmed", 0.9, "strict_exam_intent")
    store.add_marker(video_id, 0, 3000, "扩展重点", "", "这个会考。", "AI-possible", 0.6, "high_recall")
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 1)
    store.set_status(video_id, "review_ready")
    judge = ScriptedJudge(lambda calls, chunk: SemanticSelection())

    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent",
            force=True, should_stop=lambda: True,
        )

    assert judge.calls == 0
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert marker_titles(store, video_id, "high_recall") == ["扩展重点"]
    run = store.get_analysis_run(video_id, "strict_exam_intent")
    assert (run["status"], run["candidate_count"]) == ("completed", 1)
    assert store.get_video(video_id)["status"] == "review_ready"


def test_cancel_during_multi_chunk_screening_sends_no_new_chunks(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    store, video_id = make_store(tmp_path, spaced_segments(4))
    store.add_marker(video_id, 0, 500, "旧考点", "旧注释", "第 1 句", "AI-confirmed", 0.9, "strict_exam_intent")
    cancelled = threading.Event()

    def respond_then_cancel(calls, chunk):
        cancelled.set()
        return selection_for(chunk, "新考点")

    judge = ScriptedJudge(respond_then_cancel)

    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent",
            should_stop=cancelled.is_set, filter_formula_noise=False,
        )

    assert judge.calls == 1
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "failed"


def test_cancel_after_all_responses_prevents_publishing(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    segments = spaced_segments(4)
    store, video_id = make_store(tmp_path, segments)
    store.add_marker(video_id, 0, 500, "旧考点", "旧注释", "第 1 句", "AI-confirmed", 0.9, "strict_exam_intent")
    state = {"stop": False}

    def respond_to_every_chunk(calls, chunk):
        if calls == len(segments):
            state["stop"] = True
        return selection_for(chunk, "新考点")

    judge = ScriptedJudge(respond_to_every_chunk)

    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent",
            should_stop=lambda: state["stop"], filter_formula_noise=False,
        )

    assert judge.calls == 4
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "failed"


def test_request_failure_keeps_previous_markers_and_review_state(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    store, video_id = make_store(tmp_path, spaced_segments(3))
    store.add_marker(video_id, 0, 500, "旧考点", "旧注释", "第 1 句", "AI-confirmed", 0.9, "strict_exam_intent")
    store.add_marker(video_id, 0, 500, "扩展重点", "", "第 1 句", "AI-possible", 0.6, "high_recall")
    store.set_status(video_id, "review_ready")

    def fail_every_chunk(calls, chunk):
        raise ConnectionError("fixture offline")

    judge = ScriptedJudge(fail_every_chunk)

    with pytest.raises(ConnectionError):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent", filter_formula_noise=False,
        )

    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert marker_titles(store, video_id, "high_recall") == ["扩展重点"]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "failed"
    assert store.get_video(video_id)["status"] == "review_ready"


def test_retry_after_cancellation_reuses_cached_chunks_and_publishes(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    store, video_id = make_store(tmp_path, spaced_segments(4))
    cancelled = threading.Event()

    def respond_then_cancel(calls, chunk):
        cancelled.set()
        return selection_for(chunk, "新考点")

    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, ScriptedJudge(respond_then_cancel), "strict_exam_intent",
            should_stop=cancelled.is_set, filter_formula_noise=False,
        )

    retry = ScriptedJudge(lambda calls, chunk: selection_for(chunk, "新考点"))
    outcome, count = process_saved_analysis_once(
        store, video_id, retry, "strict_exam_intent", filter_formula_noise=False,
    )

    assert (outcome, count) == ("completed", 4)
    assert retry.calls == 3  # the chunk that answered before the cancel was cached
    assert marker_titles(store, video_id, "strict_exam_intent") == ["新考点"] * 4
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "completed"
    assert store.get_video(video_id)["status"] == "review_ready"


def test_force_rebuild_replaces_stale_markers_and_keeps_user_decisions(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    store, video_id = make_store(tmp_path, spaced_segments(2))
    excluded_id = store.add_marker(
        video_id, 10000, 10500, "已移除考点", "注释", "第 1 句", "AI-confirmed", 0.9, "strict_exam_intent"
    )
    store.set_marker_review_status(excluded_id, "excluded")
    kept_id = store.add_marker(
        video_id, 20000, 20500, "重要考点", "注释", "第 2 句", "AI-confirmed", 0.9, "strict_exam_intent"
    )
    store.set_marker_importance(kept_id, "important")
    store.set_marker_jump_time(kept_id, 20100)
    store.add_marker(video_id, 90000, 95000, "过期考点", "注释", "第 9 句", "AI-possible", 0.5, "strict_exam_intent")
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 3)
    judge = ScriptedJudge(lambda calls, chunk: selection_for(chunk, "新考点"))

    outcome, count = process_saved_analysis_once(store, video_id, judge, "strict_exam_intent", force=True)

    assert (outcome, count) == ("completed", 2)
    visible = {
        (marker["start_ms"], marker["end_ms"]): marker
        for marker in store.list_markers(video_id, "strict_exam_intent")
    }
    assert set(visible) == {(20000, 20500)}
    kept = visible[(20000, 20500)]
    assert kept["id"] == kept_id
    assert kept["importance"] == "important"
    assert kept["user_start_ms"] == 20100
    assert [row["id"] for row in store.list_excluded_markers(video_id)] == [excluded_id]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "completed"


def test_prepared_legacy_analysis_honours_cancellation(tmp_path):
    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.add_marker(video_id, 0, 3000, "旧考点", "旧注释", "这个会考。", "AI-confirmed", 0.9, "strict_exam_intent")
    store.set_status(video_id, "processing")
    judge = ScriptedJudge(lambda calls, chunk: SemanticSelection())

    with pytest.raises(ProcessingCancelled):
        analyze_prepared_video_once(
            store, video_id, judge, analysis_mode="strict_exam_intent", should_stop=lambda: True,
        )

    assert judge.calls == 0
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "failed"


def test_cancel_with_parallel_requests_bounds_model_calls(tmp_path, monkeypatch):
    single_segment_chunks(monkeypatch)
    store, video_id = make_store(tmp_path, spaced_segments(6))
    store.add_marker(video_id, 0, 500, "旧考点", "旧注释", "第 1 句", "AI-confirmed", 0.9, "strict_exam_intent")
    cancelled = threading.Event()

    def respond_then_cancel(calls, chunk):
        cancelled.set()
        return selection_for(chunk, "新考点")

    judge = ScriptedJudge(respond_then_cancel, concurrency=2)

    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent",
            should_stop=cancelled.is_set, filter_formula_noise=False,
        )

    # Only requests already in flight on the two workers may finish.
    assert 1 <= judge.calls <= 2
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "failed"


@pytest.mark.parametrize("first_response", ["unsupported", "malformed", "empty"])
def test_cancel_before_adapter_resend_preserves_previous_results(tmp_path, first_response):
    from classcue.openai_judge import OpenAICompatibleJudge

    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.add_marker(video_id, 0, 3000, "旧考点", "", "", "AI-confirmed", 0.9, "strict_exam_intent")
    cancelled = threading.Event()
    calls = []

    class UnsupportedFormat(Exception):
        status_code = 400

    def create(**request):
        calls.append(request)
        content = '{"candidates":[]}'
        if len(calls) == 1:
            cancelled.set()
            if first_response == "unsupported":
                raise UnsupportedFormat("response_format unsupported")
            content = "invalid json" if first_response == "malformed" else ""
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason="stop",
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    judge = OpenAICompatibleJudge(
        "synthetic-key", "synthetic", base_url="https://model.test/v1",
        mode="strict_exam_intent", client=client,
    )
    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, judge, "strict_exam_intent", should_stop=cancelled.is_set,
        )
    assert len(calls) == 1
    assert marker_titles(store, video_id, "strict_exam_intent") == ["旧考点"]


def test_rejected_publication_commit_keeps_previous_markers(tmp_path, monkeypatch):
    import sqlite3
    import classcue.video_store as video_store
    from classcue.sqlite_connection import ClosingConnection

    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.add_marker(video_id, 0, 3000, "旧考点", "保留注释", "", "AI-confirmed", 0.9, "strict_exam_intent")
    before = [dict(row) for row in store.list_markers(video_id, "strict_exam_intent")]

    class RejectMarkerCommit(ClosingConnection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if " ".join(sql.upper().split()).startswith("INSERT INTO VIDEO_MARKERS"):
                self.set_authorizer(lambda action, operation, *_args: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT"
                    else sqlite3.SQLITE_OK
                ))
            return result

    monkeypatch.setattr(video_store, "ClosingConnection", RejectMarkerCommit)
    with pytest.raises(sqlite3.DatabaseError):
        process_saved_analysis_once(
            store, video_id, ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点")),
            "strict_exam_intent", force=True,
        )
    assert [dict(row) for row in store.list_markers(video_id, "strict_exam_intent")] == before


def test_cancel_during_publication_rolls_back_marker_replacement(tmp_path, monkeypatch):
    import classcue.video_store as video_store
    from classcue.sqlite_connection import ClosingConnection

    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.add_marker(video_id, 0, 3000, "旧考点", "保留注释", "", "AI-confirmed", 0.9, "strict_exam_intent")
    before = [dict(row) for row in store.list_markers(video_id, "strict_exam_intent")]
    cancelled = threading.Event()

    class CancelAfterInsert(ClosingConnection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if " ".join(sql.upper().split()).startswith("INSERT INTO VIDEO_MARKERS"):
                cancelled.set()
            return result

    monkeypatch.setattr(video_store, "ClosingConnection", CancelAfterInsert)
    with pytest.raises(ProcessingCancelled):
        process_saved_analysis_once(
            store, video_id, ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点")),
            "strict_exam_intent", force=True, should_stop=cancelled.is_set,
        )
    assert cancelled.is_set()
    assert [dict(row) for row in store.list_markers(video_id, "strict_exam_intent")] == before


@pytest.fixture
def real_sdk_judge():
    import httpx
    from openai import OpenAI
    from classcue.openai_judge import OpenAICompatibleJudge

    clients = []

    def make(handler, *, max_retries=1, profile_retries=None):
        client = OpenAI(
            api_key="synthetic-key", base_url="https://model.test/v1",
            max_retries=max_retries, timeout=7,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        judge = OpenAICompatibleJudge(
            "synthetic-key", "synthetic", base_url="https://model.test/v1",
            mode="strict_exam_intent", client=client,
            max_retries=max_retries if profile_retries is None else profile_retries,
        )
        return judge, client

    yield make
    for client in clients:
        client.close()


def sdk_response(request, *, status=200, content='{"candidates":[]}', headers=None):
    import httpx

    payload = {"error": {"message": content, "type": "synthetic_error"}}
    if status == 200:
        payload = {
            "id": "synthetic-completion", "object": "chat.completion",
            "created": 0, "model": "synthetic",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
        }
    return httpx.Response(status, json=payload, headers=headers, request=request)


def sdk_retry_failure(request, failure):
    import httpx

    if failure == "timeout":
        raise httpx.ReadTimeout("synthetic timeout", request=request)
    if failure == "connection":
        raise httpx.ConnectError("synthetic connection failure", request=request)
    return sdk_response(
        request, status=failure, content="synthetic retryable error",
        headers={"retry-after-ms": "1"},
    )


@pytest.mark.parametrize("failure", [408, 409, 429, 500, 503, "timeout", "connection"])
def test_real_sdk_retry_stops_after_cancellation(real_sdk_judge, failure):
    cancelled = threading.Event()
    sends = []

    def handle(request):
        sends.append(cancelled.is_set())
        if len(sends) == 1:
            cancelled.set()
            return sdk_retry_failure(request, failure)
        return sdk_response(request)

    judge, _client = real_sdk_judge(handle)
    with pytest.raises(ProcessingCancelled):
        judge.judge_semantic_chunk(
            (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=cancelled.is_set,
        )
    assert sends == [False], "no retry transport may start after cancellation"


def test_real_sdk_cancellation_interrupts_retry_wait(real_sdk_judge):
    import time

    cancelled = threading.Event()
    sends = []
    timer = threading.Timer(0.05, cancelled.set)

    def handle(request):
        sends.append(request)
        if len(sends) == 1:
            timer.start()
            return sdk_response(
                request, status=429, headers={"retry-after": "2"},
            )
        return sdk_response(request)

    judge, _client = real_sdk_judge(handle)
    started = time.monotonic()
    try:
        with pytest.raises(ProcessingCancelled):
            judge.judge_semantic_chunk(
                (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=cancelled.is_set,
            )
        assert time.monotonic() - started < 1.5, "retry wait must observe cancellation"
        assert len(sends) == 1
    finally:
        timer.cancel()
        timer.join(timeout=3)


@pytest.mark.parametrize("failure", [429, "timeout"])
@pytest.mark.parametrize("cancellable", [False, True])
def test_real_sdk_retries_normally_and_preserves_borrowed_client(
    real_sdk_judge, failure, cancellable,
):
    sends = []

    def handle(request):
        sends.append(request)
        if len(sends) == 1:
            return sdk_retry_failure(request, failure)
        return sdk_response(request)

    judge, client = real_sdk_judge(handle, max_retries=1, profile_retries=5)
    timeout = client.timeout
    judge.judge_semantic_chunk(
        (TranscriptSegment(1, 0, 3000, "这个会考。"),),
        should_stop=(lambda: False) if cancellable else None,
    )
    assert len(sends) == 2
    assert client.max_retries == 1 and client.timeout == timeout
    assert all(request.extensions["timeout"]["read"] == 7 for request in sends)
    judge.close()
    assert not client.is_closed()


def test_real_sdk_retry_exhaustion_keeps_configured_limit(real_sdk_judge):
    from openai import RateLimitError

    sends = []

    def handle(request):
        sends.append(request)
        return sdk_retry_failure(request, 429)

    judge, _client = real_sdk_judge(handle, max_retries=2, profile_retries=5)
    with pytest.raises(RateLimitError):
        judge.judge_semantic_chunk(
            (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=lambda: False,
        )
    assert len(sends) == 3


@pytest.mark.parametrize(("status", "headers", "expected_sends"), [
    (503, {"x-should-retry": "false"}, 1),
    (418, {"x-should-retry": "true", "retry-after-ms": "1"}, 2),
    (429, {"retry-after": "121"}, 1),
])
def test_real_sdk_retry_honors_server_directives(real_sdk_judge, status, headers, expected_sends):
    from openai import APIStatusError

    sends = []

    def handle(request):
        sends.append(request)
        if len(sends) == 1:
            return sdk_response(request, status=status, headers=headers)
        return sdk_response(request)

    judge, _client = real_sdk_judge(handle)
    if expected_sends == 1:
        with pytest.raises(APIStatusError):
            judge.judge_semantic_chunk(
                (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=lambda: False,
            )
    else:
        judge.judge_semantic_chunk(
            (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=lambda: False,
        )
    assert len(sends) == expected_sends


def test_real_sdk_json_fallback_and_repair_still_work(real_sdk_judge):
    import json

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return sdk_response(request, status=400, content="response_format is unsupported")
        return sdk_response(request, content="invalid JSON" if len(requests) == 2 else '{"candidates":[]}')

    judge, _client = real_sdk_judge(handle)
    assert not judge.judge_semantic_chunk(
        (TranscriptSegment(1, 0, 3000, "这个会考。"),), should_stop=lambda: False,
    ).candidates
    assert len(requests) == 3
    assert "response_format" in requests[0] and "response_format" not in requests[1]
    assert "response_format" in requests[2], "format repair remains a separate request"
