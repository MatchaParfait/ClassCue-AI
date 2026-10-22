import copy
import json
import re
from threading import Condition
from types import SimpleNamespace

import pytest

from classcue.ai_provider import ProviderProfile, create_judge
from classcue.core import TranscriptSegment
from classcue.exam_point_judge import ExamPointJudge
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore


class ConcurrentClient:
    max_retries = 0

    def __init__(self, concurrency, *, repair=False, fail_first=False, cancel=False):
        self.concurrency = concurrency
        self.condition = Condition()
        self.active = 0
        self.peak = 0
        self.first_wave = 0
        self.requests = []
        self.repair = repair
        self.fail_first = fail_first
        self.cancel = cancel
        self.cancelled = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        content = request["messages"][-1]["content"]
        transcript = content.rsplit("<transcript_text>", 1)[1].split("</transcript_text>", 1)[0]
        index = int(re.search(r"\{(\d+)\}", transcript).group(1))
        repairing = "<invalid_output_json>" in content
        with self.condition:
            self.requests.append(copy.deepcopy(request))
            self.active += 1
            self.peak = max(self.peak, self.active)
            if not repairing and len(self.requests) <= self.concurrency:
                self.first_wave += 1
                self.condition.notify_all()
                assert self.condition.wait_for(
                    lambda: self.first_wave >= self.concurrency, timeout=5,
                ), "Configured independent requests did not overlap"
        try:
            if self.fail_first and index == 1:
                raise ConnectionError("fixture transport failure")
            if self.cancel:
                self.cancelled = True
            if self.repair and index == 1 and not repairing:
                raw = "invalid-json"
            else:
                raw = json.dumps({"candidates": [{
                    "level": "confirmed", "signal_type": "explicit_exam",
                    "start_sentence_id": index, "end_sentence_id": index,
                    "evidence_sentence_ids": [index], "title": f"point-{index}",
                    "reason": "explicit instruction", "evidence_quote": f"Lesson {index} will be tested.",
                }]})
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=raw))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10, total_tokens=20),
                id=f"fixture-{index}", model="fixture",
            )
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


@pytest.fixture
def lesson(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    video_id = store.create_video("fixture", "lesson.mp4", "lesson.mp4", "audio.wav", 3600000)
    segments = [
        TranscriptSegment(index, (index - 1) * 610000, (index - 1) * 610000 + 1000,
                          f"Lesson {index} will be tested.")
        for index in range(1, 6)
    ]
    store.mark_transcribed(video_id, segments)
    return store, video_id


def judge(concurrency, client):
    profile = ProviderProfile(
        base_url="https://unused.test/v1", model="fixture", max_retries=0,
        request_concurrency=concurrency,
    )
    return ExamPointJudge(profile, "fixture-key", mode="medium_recall", client=client)


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_production_chunks_overlap_up_to_limit_and_publish_deterministically(lesson, concurrency):
    store, video_id = lesson
    client = ConcurrentClient(concurrency)
    events = []
    result = judge(concurrency, client).analyze_video(
        store, video_id, filter_formula_noise=False,
        progress=lambda value, message: events.append((value, message)),
    )
    assert result == ("completed", 5)
    assert client.peak == concurrency
    assert client.active == 0 and len(client.requests) == 5
    markers = store.list_markers(video_id, "medium_recall")
    assert [item["title"] for item in markers] == [f"point-{index}" for index in range(1, 6)]
    row = store.get_analysis_run(video_id, "medium_recall")
    report = store.get_experiment_results(row["experiment_id"])
    assert report["usage"]["totals"]["total_tokens"] == 100
    assert len(store.list_experiment_calls(row["experiment_id"])) == 5
    assert events[-1][0] == 1 and str(concurrency) in events[-1][1]
    assert "wall_seconds" in report
    assert all("request_concurrency" not in request for request in client.requests)


def test_repair_requests_share_the_chunk_limit_and_keep_call_lineage(lesson):
    store, video_id = lesson
    client = ConcurrentClient(3, repair=True)
    assert judge(3, client).analyze_video(store, video_id, filter_formula_noise=False) == ("completed", 5)
    assert client.peak <= 3 and len(client.requests) == 6
    row = store.get_analysis_run(video_id, "medium_recall")
    calls = store.list_experiment_calls(row["experiment_id"])
    repair = next(call for call in calls if call["stage"] == "json_repair")
    assert repair["parent_call_id"] in {call["call_id"] for call in calls if call["stage"] == "extract"}
    assert store.get_experiment_results(row["experiment_id"])["format_repair_count"] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_or_cancellation_stops_new_chunks_and_keeps_old_markers(lesson, cancel):
    store, video_id = lesson
    store.add_marker(video_id, 0, 500, "previous", "", "", "AI-confirmed", .9, "medium_recall")
    client = ConcurrentClient(2, fail_first=not cancel, cancel=cancel)
    adapter = judge(2, client)
    with pytest.raises(ProcessingCancelled if cancel else ConnectionError):
        adapter.analyze_video(
            store, video_id, filter_formula_noise=False,
            should_stop=lambda: client.cancelled,
        )
    assert client.active == 0
    assert len(client.requests) <= 2
    assert [row["title"] for row in store.list_markers(video_id, "medium_recall")] == ["previous"]
    log = store.list_experiments(video_id)[-1]
    assert log["status"] == ("interrupted" if cancel else "failed")
    assert all(call["status"] != "started" for call in store.list_experiment_calls(log["id"]))


def test_changing_concurrency_does_not_send_extra_http_parameters(lesson):
    store, video_id = lesson
    client = ConcurrentClient(2)
    judge(2, client).analyze_video(store, video_id, filter_formula_noise=False)
    assert all(not any("concurrency" in key for key in request) for request in client.requests)


@pytest.mark.parametrize("mode", ["medium_recall", "strict_exam_intent"])
def test_factory_preserves_concurrency_for_production_and_legacy_modes(mode):
    profile = ProviderProfile(
        base_url="https://unused.test/v1", model="fixture", request_concurrency=4,
    )
    adapter = create_judge(profile, "fixture-key", mode=mode, client=object())
    assert adapter.profile.request_concurrency == 4
