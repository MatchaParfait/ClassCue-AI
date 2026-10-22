"""FireRed overlap must preserve the production request and publication contract."""

import copy
import json
import re
from threading import Event
from types import SimpleNamespace

import pytest

from classcue.ai_provider import ProviderProfile
from classcue.api_failure import ApiUnavailable
from classcue.core import TranscriptSegment
from classcue.early_extraction import early_analysis
from classcue.exam_point_judge import ExamPointJudge
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.transcript_events import emit_transcript, transcript_requested
from classcue.video_pipeline import process_video_once
from classcue.video_store import VideoStore


def supplied_ids(request):
    text = request["messages"][-1]["content"].rsplit("<transcript_text>", 1)[1]
    return [int(value) for value in re.findall(r"\{(\d+)\}", text)]


class Client:
    max_retries = 0

    def __init__(self, respond=None):
        self.requests = []
        self.started = Event()
        self.respond = respond or self.candidates
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @staticmethod
    def candidates(request):
        ids = supplied_ids(request)
        # The two overlapping blocks select the same evidence.
        selected = 117 if 117 in ids else ids[0]
        return {"candidates": [{
            "level": "confirmed", "signal_type": "explicit_exam",
            "start_sentence_id": selected, "end_sentence_id": selected,
            "evidence_sentence_ids": [selected], "title": f"考点{selected}",
            "reason": "教师明确说明会考", "evidence_quote": text(selected),
        }]}

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        self.started.set()
        content = self.respond(request)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=content),
            )],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            id=f"response-{len(self.requests)}", model="test",
        )


def text(index):
    return f"第{index}段课程的排序知识会考，必须掌握具体步骤并注意常见错误。"


@pytest.fixture
def lesson(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    path = tmp_path / "lesson.mp4"
    path.touch()
    video_id = store.create_video("lesson", path.name, str(path), str(tmp_path / "audio.wav"), 1500000)
    profile = ProviderProfile(
        base_url="https://unused.example/v1", model="test", max_tokens=4096, max_retries=0,
    )
    segments = [
        TranscriptSegment(index, (index - 1) * 5000, index * 5000, text(index), [])
        for index in range(1, 301)
    ]
    return SimpleNamespace(store=store, video_id=video_id, profile=profile, segments=segments)


def adapter(lesson, client=None, mode="medium_recall"):
    return ExamPointJudge(lesson.profile, "fixture-secret", mode=mode, client=client or Client())


def test_validated_chunk_preview_precedes_publication_and_retains_overlap(lesson):
    from classcue.analysis_events import analysis_observer
    lesson.store.mark_transcribed(lesson.video_id, lesson.segments)
    events = []
    def preview(video_id, mode, **state):
        assert video_id == lesson.video_id and mode == "medium_recall"
        assert lesson.store.list_markers(video_id) == []
        events.append(copy.deepcopy(state))
    client = Client()
    with analysis_observer(preview):
        status, count = adapter(lesson, client).analyze_video(lesson.store, lesson.video_id, filter_formula_noise=False)
    assert status == "completed"
    assert len(client.requests) >= 3
    assert events[0]["completed"] == 0 and events[0]["current"] == 1 and events[0]["results"] == []
    completed = [e for e in events if e["current"] is None]
    assert [e["completed"] for e in completed] == list(range(1, len(client.requests) + 1))
    assert completed[0]["results"][0]["source_quote"] == text(117)
    assert len(completed[-1]["results"]) == count
    assert events[2]["current"] == 2 and events[2]["results"] == completed[0]["results"]


def test_failed_chunk_keeps_only_previously_validated_preview(lesson):
    from classcue.analysis_events import analysis_observer
    lesson.store.mark_transcribed(lesson.video_id, lesson.segments)
    client, events = Client(), []
    def respond(request):
        if len(client.requests) > 1:
            raise RuntimeError("API unavailable")
        return Client.candidates(request)
    client.respond = respond
    with analysis_observer(lambda *args, **state: events.append(copy.deepcopy(state))):
        with pytest.raises(RuntimeError, match="API unavailable"):
            adapter(lesson, client).analyze_video(lesson.store, lesson.video_id, filter_formula_noise=False)
    assert events[-1]["completed"] == 1 and events[-1]["current"] == 2
    assert len(events[-1]["results"]) == 1
    assert lesson.store.list_markers(lesson.video_id) == []


def test_empty_chunk_preview_is_real_progress_not_fake_hits(lesson):
    from classcue.analysis_events import analysis_observer
    lesson.store.mark_transcribed(lesson.video_id, lesson.segments)
    events = []
    with analysis_observer(lambda *args, **state: events.append(copy.deepcopy(state))):
        status, count = adapter(lesson, Client(lambda _: {"candidates": []})).analyze_video(
            lesson.store, lesson.video_id, filter_formula_noise=False)
    assert status == "completed" and count == 0
    assert events[-1]["completed"] == events[-1]["total"] > 1
    assert all(event["results"] == [] for event in events)


def process(lesson, judge, **options):
    return process_video_once(
        lesson.store, lesson.video_id, judge,
        asr_model_id=options.pop("asr_model_id", "fireredasr2-aed"),
        analysis_mode=judge.mode, filter_formula_noise=False, **options,
    )


def markers(lesson):
    return [
        {key: item[key] for key in (
            "start_ms", "end_ms", "title", "source_quote", "analysis_mode",
        )} for item in lesson.store.list_markers(lesson.video_id)
    ]


@pytest.mark.parametrize("concurrency", [1, 3])
def test_request_begins_before_asr_finishes_and_final_requests_and_dedup_match_serial(
    lesson, monkeypatch, concurrency,
):
    import classcue.video_pipeline as video_pipeline

    client = Client()
    lesson.profile.request_concurrency = concurrency
    asr_finished = Event()
    def respond(request):
        if len(client.requests) == 1:
            assert not asr_finished.is_set()
        return Client.candidates(request)
    client.respond = respond

    def transcribe(*_args, **_kwargs):
        assert transcript_requested()
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3), "First LLM request must not wait for ASR completion"
        # No partial transcript or candidate is published during ASR.
        assert not lesson.store.get_segments(lesson.video_id)
        assert not markers(lesson)
        emit_transcript(lesson.segments[140:260], 1300000)
        asr_finished.set()
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client)) == ("completed", 2)
    early_requests, early_markers = list(client.requests), markers(lesson)
    assert len(early_requests) == 3
    run = lesson.store.get_analysis_run(lesson.video_id, "medium_recall")
    metadata = json.loads(lesson.store.get_experiment(run["experiment_id"])["options_json"])
    assert metadata["snapshot_hash"] == run["analysis_signature"]
    assert metadata["input_stats"]["source_segment_count"] == len(lesson.segments)
    assert len(lesson.store.get_transcript_snapshot(metadata["transcript_hash"])["segments"]) == 300
    calls = lesson.store.list_experiment_calls(run["experiment_id"])
    assert len(calls) == 3
    assert calls[0].get("early_extraction_key")
    assert len(lesson.store.get_transcript_snapshot(calls[0]["early_transcript_hash"])["segments"]) == 140
    assert len({call["call_id"] for call in calls}) == 3
    report = lesson.store.get_experiment_results(run["experiment_id"])
    assert report["call_count"] == 3
    assert report["usage"]["totals"]["total_tokens"] == 360

    baseline = Client()
    # Reanalysis from the saved transcript may send requests in a different
    # order, but the actual input blocks and final evidence must be identical.
    judge = adapter(lesson, baseline)
    assert judge.analyze_video(
        lesson.store, lesson.video_id, force=True, filter_formula_noise=False,
    ) == ("completed", 2)
    assert sorted(baseline.requests, key=lambda item: supplied_ids(item)[0]) == sorted(
        early_requests, key=lambda item: supplied_ids(item)[0],
    )
    assert markers(lesson) == early_markers


@pytest.mark.parametrize("model", [
    "whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b", "paraformer-large-cpu", "dolphin-small-cn",
])
def test_other_asr_engines_do_not_enable_early_requests(lesson, monkeypatch, model):
    import classcue.video_pipeline as video_pipeline
    client = Client()
    def transcribe(*_args, **_kwargs):
        assert not transcript_requested()
        assert not client.requests
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client), asr_model_id=model)[0] == "completed"


def test_short_video_stays_on_normal_path(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    client = Client()
    short = lesson.segments[:50]
    def transcribe(*_args, **_kwargs):
        emit_transcript(short, 250000)
        assert not client.requests
        return short, 250000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    assert len(client.requests) == 1
    run = lesson.store.get_analysis_run(lesson.video_id, "medium_recall")
    assert not lesson.store.list_experiment_calls(run["experiment_id"])[0].get("early_extraction_key")


def test_long_leading_silence_does_not_fail_the_later_transcript(lesson):
    client = Client()
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False,
    ):
        emit_transcript([], 700000)
        late = [TranscriptSegment(1, 710000, 715000, text(1))]
        emit_transcript(late, 715000)
        judge._early_extraction.finish()
        assert not client.requests
        lesson.store.mark_transcribed(lesson.video_id, late, "fireredasr2-aed")
        assert judge.analyze_video(
            lesson.store, lesson.video_id, filter_formula_noise=False,
        )[0] == "completed"
    assert len(client.requests) == 1


def test_tail_is_not_sent_early_and_final_snapshot_keeps_overlap(lesson):
    client = Client()
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False,
    ):
        emit_transcript(lesson.segments[:140], 700000)
        judge._early_extraction.finish()
        assert len(client.requests) == 1
        assert supplied_ids(client.requests[0]) == list(range(1, 121))
        lesson.store.mark_transcribed(lesson.video_id, lesson.segments, "fireredasr2-aed")
        judge.analyze_video(lesson.store, lesson.video_id, filter_formula_noise=False)
        assert len(client.requests) == 3
        assert set(supplied_ids(client.requests[0])) & set(supplied_ids(client.requests[1])) == set(range(114, 121))


def test_keyword_filter_does_not_mistake_last_selected_chunk_for_unfinished_tail(lesson):
    # Only the first block has the requested keyword; later blocks are filtered.
    lesson.segments = [
        TranscriptSegment(
            item.id, item.start_ms, item.end_ms,
            item.text + (" 唯一信号" if item.id == 1 else ""),
        ) for item in lesson.segments
    ]
    client = Client(lambda _request: {"candidates": []})
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False, use_keyword_prefilter=True,
        trigger_groups={"custom": {"user": ["唯一信号"]}},
    ):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        judge._early_extraction.finish()
        assert len(client.requests) == 1
        lesson.store.mark_transcribed(lesson.video_id, lesson.segments, "fireredasr2-aed")
        judge.analyze_video(
            lesson.store, lesson.video_id, filter_formula_noise=False,
            use_keyword_prefilter=True, trigger_groups={"custom": {"user": ["唯一信号"]}},
        )
        assert len(client.requests) == 1


def test_failed_early_request_preserves_transcript_and_is_not_automatically_sent_again(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    client = Client(lambda _request: (_ for _ in ()).throw(ConnectionError("fixture-secret offline")))
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    with pytest.raises(ApiUnavailable):
        process(lesson, adapter(lesson, client))
    assert len(client.requests) == 1
    assert lesson.store.get_segments(lesson.video_id) == lesson.segments
    assert not markers(lesson)
    assert all("fixture-secret" not in row["error_message"]
               for row in lesson.store.list_experiments(lesson.video_id))


def test_early_malformed_response_uses_existing_format_repair(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    client = Client()
    def respond(request):
        return "not JSON" if len(client.requests) == 1 else Client.candidates(request)
    client.respond = respond
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    row = lesson.store.get_analysis_run(lesson.video_id, "medium_recall")
    report = lesson.store.get_experiment_results(row["experiment_id"])
    assert report["format_repair_count"] == 1
    assert report["call_count"] == 4


@pytest.mark.parametrize("failure", [
    ProcessingCancelled("stop"), ResourcePaused("release GPU"),
])
def test_asr_interruption_does_not_publish_early_results_and_resume_reuses_calls(
    lesson, monkeypatch, failure,
):
    import classcue.video_pipeline as video_pipeline
    client = Client()
    def interrupted(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        # Complete the response before pausing the ASR.
        judge._early_extraction.finish()
        raise failure
    judge = adapter(lesson, client)
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", interrupted)
    with pytest.raises(type(failure)):
        process(lesson, judge)
    assert not lesson.store.get_segments(lesson.video_id)
    assert not markers(lesson)
    assert len(client.requests) == 1
    partial = lesson.store.list_experiments(lesson.video_id)
    assert len(partial) == 1 and partial[0]["status"] == "interrupted"

    def resumed(*_args, **_kwargs):
        # A verified full FireRed cache can return without incremental events.
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", resumed)
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    assert len(client.requests) == 3
    assert len(lesson.store.list_experiments(lesson.video_id)) == 1


def test_abandoned_prefetch_log_is_reused_after_unexpected_exit(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    client = Client()
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False,
    ):
        emit_transcript(lesson.segments[:140], 700000)
        judge._early_extraction.finish()
    row = lesson.store.list_experiments(lesson.video_id)[0]
    # An abrupt process exit leaves this status instead of running close().
    lesson.store.update_experiment(row["id"], status="prefetching")
    monkeypatch.setattr(
        video_pipeline, "_transcribe_video_with_fallback",
        lambda *_a, **_k: (lesson.segments, 1500000),
    )
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    assert len(client.requests) == 3
    assert len(lesson.store.list_experiments(lesson.video_id)) == 1


def test_early_transport_retry_uses_existing_limit_and_preserves_logged_usage(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    lesson.profile = lesson.profile.model_copy(update={"max_retries": 1})
    client = Client()
    def respond(request):
        if len(client.requests) == 1:
            raise ConnectionError("transient")
        return Client.candidates(request)
    client.respond = respond
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    row = lesson.store.get_analysis_run(lesson.video_id, "medium_recall")
    report = lesson.store.get_experiment_results(row["experiment_id"])
    assert len(client.requests) == report["call_count"] == 4
    assert report["transport_retry_count"] == 1
    assert report["usage"]["totals"]["total_tokens"] == 360
    calls = lesson.store.list_experiment_calls(row["experiment_id"])
    assert calls[1]["parent_call_id"] == calls[0]["call_id"]
    assert calls[1]["attempt"] == 2


def test_cancel_during_early_request_never_publishes_or_sends_another_request(lesson, monkeypatch):
    import classcue.video_pipeline as video_pipeline
    stopped = Event()
    client = Client()
    def respond(request):
        stopped.set()
        return Client.candidates(request)
    client.respond = respond
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    with pytest.raises(ProcessingCancelled):
        process(lesson, adapter(lesson, client), should_stop=stopped.is_set)
    assert len(client.requests) == 1
    assert not markers(lesson)
    assert lesson.store.get_segments(lesson.video_id)
    stopped.clear()
    monkeypatch.setattr(
        video_pipeline, "_transcribe_video_with_fallback",
        lambda *_a, **_k: pytest.fail("Saved ASR must not run again"),
    )
    assert process(lesson, adapter(lesson, client))[0] == "completed"
    assert len(client.requests) == 3


def test_replayed_asr_prefix_does_not_duplicate_requests(lesson):
    client = Client()
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False,
    ):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        emit_transcript(lesson.segments[:140], 700000)
        emit_transcript(lesson.segments[140:260], 1300000)
        judge._early_extraction.finish()
        assert len(client.requests) == 2
        lesson.store.mark_transcribed(lesson.video_id, lesson.segments, "fireredasr2-aed")
        judge.analyze_video(lesson.store, lesson.video_id, filter_formula_noise=False)
        assert len(client.requests) == 3


def test_final_transcript_change_does_not_reuse_a_different_request(lesson):
    client = Client(lambda _request: {"candidates": []})
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=False,
    ):
        emit_transcript(lesson.segments[:140], 700000)
        judge._early_extraction.finish()
        assert len(client.requests) == 1
        changed = list(lesson.segments)
        changed[0] = TranscriptSegment(1, 0, 5000, "字幕已修正，不能复用旧请求的内容。")
        lesson.store.mark_transcribed(lesson.video_id, changed, "fireredasr2-aed")
        judge.analyze_video(lesson.store, lesson.video_id, filter_formula_noise=False)
        assert len(client.requests) == 4
        assert client.requests[0] != client.requests[1]


def test_forced_retranscription_starts_early_but_preserves_old_results_until_asr_succeeds(
    lesson, monkeypatch,
):
    import classcue.video_pipeline as video_pipeline

    lesson.store.mark_transcribed(lesson.video_id, lesson.segments, "fireredasr2-aed")
    before = Client()
    assert adapter(lesson, before).analyze_video(
        lesson.store, lesson.video_id, filter_formula_noise=False,
    )[0] == "completed"
    previous = markers(lesson)
    client = Client()
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        assert markers(lesson) == previous
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert process(lesson, adapter(lesson, client), force_retranscribe=True)[0] == "completed"
    assert len(client.requests) == 3
    assert markers(lesson) == previous


@pytest.mark.parametrize("filter_noise", [False, True])
def test_short_sentence_merging_and_formula_filter_match_full_transcript(lesson, filter_noise):
    # Short clauses and formula noise exercise the future-sensitive input view.
    lesson.segments = [
        TranscriptSegment(
            item.id, item.start_ms, item.end_ms,
            "x 加 一 等于 二" if item.id % 17 == 0 else ("排序" if item.id % 3 else "必须掌握会考"),
        ) for item in lesson.segments
    ]
    client = Client(lambda _request: {"candidates": []})
    judge = adapter(lesson, client)
    with early_analysis(
        judge, lesson.store, lesson.video_id, asr_model_id="fireredasr2-aed",
        filter_formula_noise=filter_noise,
    ):
        emit_transcript(lesson.segments[:140], 700000)
        assert client.started.wait(3)
        emit_transcript(lesson.segments[140:260], 1300000)
        lesson.store.mark_transcribed(lesson.video_id, lesson.segments, "fireredasr2-aed")
        judge.analyze_video(lesson.store, lesson.video_id, filter_formula_noise=filter_noise)
    baseline = Client(lambda _request: {"candidates": []})
    adapter(lesson, baseline).analyze_video(
        lesson.store, lesson.video_id, force=True, filter_formula_noise=filter_noise,
    )
    assert client.requests == baseline.requests


@pytest.mark.parametrize("offline", [False, True])
def test_web_batch_preserves_modes_and_saves_asr_when_early_api_fails(lesson, monkeypatch, offline):
    import classcue.video_pipeline as video_pipeline
    from api.main import VideoProcessPayload
    from classcue.web_batch import make_batch, run_web_batch

    options = VideoProcessPayload(
        asr_model_id="fireredasr2-aed", analysis_modes=["medium_recall", "low_recall"],
    )
    clients = {}
    def factory(mode):
        client = Client(
            (lambda _request: (_ for _ in ()).throw(ConnectionError("offline")))
            if offline else None,
        )
        clients[mode] = client
        return adapter(lesson, client, mode)
    def transcribe(*_args, **_kwargs):
        emit_transcript(lesson.segments[:140], 700000)
        assert clients["medium_recall"].started.wait(3)
        assert "low_recall" not in clients
        return lesson.segments, 1500000
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    state = make_batch({"folders": []}, options.model_dump(), registered=[
        {"video_id": lesson.video_id, "source_path": "", "source_root": "",
         "status": "pending_asr", "error": ""},
    ])
    result = run_web_batch(
        lesson.store, state, options, save=lambda: None, update=lambda *_args: None,
        should_cancel=lambda: False, import_video=lambda *_args, **_kwargs: pytest.fail("No import"),
        judge_factory=factory,
    )
    assert lesson.store.get_segments(lesson.video_id) == lesson.segments
    if offline:
        assert result["job_status"] == "awaiting_api"
        assert len(clients["medium_recall"].requests) == 1
        assert not markers(lesson)
    else:
        assert result["job_status"] == "completed"
        assert len(clients["medium_recall"].requests) == 3
        assert len(clients["low_recall"].requests) == 3
        assert len(lesson.store.list_markers(lesson.video_id, "medium_recall")) == 2
        assert len(lesson.store.list_markers(lesson.video_id, "low_recall")) == 2
