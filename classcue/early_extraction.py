"""Overlap production extraction requests with ASR without changing final analysis."""

from contextlib import contextmanager
import json
from threading import Condition, Event, Thread

from classcue.llm_experiments import (
    LoggedClient, digest, prepare_snapshot, register_run, require_approval,
)
from classcue.processing_control import ProcessingCancelled
from classcue.transcript_events import transcript_listener
from classcue.asr_models import get_model, selected_model_id


def authorize_snapshot(snapshot, mode):
    snapshot["authorization"] = {"kind": "production_processing_request", "mode": mode}
    snapshot["snapshot_hash"] = digest({
        key: value for key, value in snapshot.items() if key != "snapshot_hash"
    })
    return snapshot


def extraction_key(snapshot, prepared):
    # The prefix includes filtered boundaries and source IDs, not just visible text.
    last_id = max(prepared["sentence_ids"])
    return digest({
        "runner": snapshot["runner_code_hash"],
        "profile": snapshot["profile"], "config": snapshot["config"],
        "bundle": snapshot["prompt_bundle"]["version"],
        "input": [item for item in snapshot["input_segments"] if item["id"] <= last_id],
        "request": prepared["request"],
    })


class EarlyExtraction:
    def __init__(self, judge, store, video_id, config, *, should_stop=None):
        self.judge, self.store, self.video_id, self.config = judge, store, video_id, config
        self.should_stop = should_stop or (lambda: False)
        self.condition = Condition()
        self.cancelled = Event()
        self.segments = []
        self.latest = None
        self.closed = False
        self.thread = None
        self.run_id = None
        self.completed = {}
        self.failure = None
        self.last_watermark = 0
        self.transcription_model = None
        self.allow_resume = True
        self.resume_checked = False

    def _resume(self, snapshot):
        if self.resume_checked:
            return
        self.resume_checked = True
        if not self.allow_resume:
            return
        for row in reversed(self.store.list_experiments(self.video_id)):
            if row["name"] != f"production:{self.judge.mode}" or row["status"] not in {
                "interrupted", "prefetching",
            }:
                continue
            metadata = json.loads(row["options_json"])
            if (
                metadata.get("authorization", {}).get("kind") != "production_processing_request"
                or metadata.get("profile") != snapshot["profile"]
                or metadata.get("config") != snapshot["config"]
                or metadata.get("runner_code_hash") != snapshot["runner_code_hash"]
                or metadata.get("prompt_bundle", {}).get("version") != self.judge.bundle["version"]
                or self.store.get_experiment_results(row["id"]) is not None
            ):
                continue
            calls = self.store.list_experiment_calls(row["id"])
            if not any("early_extraction_key" in call for call in calls):
                continue
            self.run_id = row["id"]
            for call in calls:
                if call.get("early_extraction_key") and call["status"] == "completed":
                    raw = call.get("raw_content")
                    if isinstance(raw, str):
                        self.completed[call["early_extraction_key"]] = (raw, call["call_id"])
            self.store.update_experiment(self.run_id, status="prefetching", error_message="")
            return

    def stopped(self):
        return self.cancelled.is_set() or self.should_stop()

    def feed(self, segments, settled_until_ms):
        with self.condition:
            if segments and self.segments and segments[0].id <= self.segments[-1].id:
                # A worker retry can replay its durable prefix; request keys
                # still prevent an identical chunk from being sent twice.
                self.segments.clear()
                self.last_watermark = 0
            self.segments.extend(segments)
            # Coalesce frequent ASR events; preparation and HTTP never block ASR.
            if not self.segments:
                return
            if settled_until_ms < self.config.chunk_seconds * 1000:
                return
            if settled_until_ms < self.last_watermark + 10000:
                return
            self.last_watermark = settled_until_ms
            self.latest = (list(self.segments), settled_until_ms)
            if self.thread is None:
                self.thread = Thread(
                    target=self._work, name="early-llm-extraction", daemon=True,
                )
                self.thread.start()
            self.condition.notify()

    def _work(self):
        try:
            while not self.stopped():
                with self.condition:
                    while self.latest is None and not self.closed and not self.stopped():
                        self.condition.wait(timeout=.2)
                    if self.stopped() or (self.closed and self.latest is None):
                        return
                    segments, watermark = self.latest
                    self.latest = None
                snapshot = authorize_snapshot(prepare_snapshot(
                    self.store, self.video_id, self.config, self.judge.profile,
                    bundle=self.judge.bundle, allow_empty=True, segments=segments,
                    transcription_model=self.transcription_model,
                    closed_chunks_only=True,
                ), self.judge.mode)
                # Filtering can retain the previous 20 seconds when new context
                # arrives; input merging can extend a sentence by another 30.
                stable_before = watermark - (
                    (20 if self.config.filter_formula_noise else 0)
                    + self.config.merge_max_span_seconds
                ) * 1000
                by_id = {item["id"]: item for item in snapshot["input_segments"]}
                for prepared in snapshot["requests"]:
                    if self.stopped():
                        return
                    last = by_id[max(prepared["sentence_ids"])]
                    if last["end_ms"] >= stable_before:
                        continue
                    key = extraction_key(snapshot, prepared)
                    self._resume(snapshot)
                    if key in self.completed:
                        continue
                    if self.run_id is None:
                        self.run_id = register_run(
                            self.store, f"production:{self.judge.mode}", snapshot,
                            snapshot["snapshot_hash"],
                        )
                        self.store.update_experiment(self.run_id, status="prefetching")
                    self.store.save_transcript_snapshot(
                        snapshot["transcript_hash"], snapshot["transcript"],
                    )
                    try:
                        logged = LoggedClient(
                            self.store, self.run_id, self.judge.profile, self.config,
                            self.judge._api_key, client=self.judge.client,
                            should_cancel=self.stopped,
                            call_metadata={
                                "early_extraction_key": key,
                                "early_snapshot_hash": snapshot["snapshot_hash"],
                                "early_transcript_hash": snapshot["transcript_hash"],
                                "early_input_view_hash": snapshot["input_view_hash"],
                            },
                        )
                        try:
                            self.completed[key] = logged.request(
                                prepared["request"], prepared["chunk_id"], "extract",
                            )
                        finally:
                            logged.close()
                    except BaseException as error:
                        self.failure = (key, error)
                        return
        except BaseException as error:
            self.failure = (None, error)

    def finish(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        if self.should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        if self.failure is not None and self.failure[0] is None:
            raise self.failure[1]

    def bind(self, snapshot):
        self.finish()
        self._resume(snapshot)
        if self.run_id is None:
            return None
        require_approval(snapshot, snapshot["snapshot_hash"])
        keys = {extraction_key(snapshot, item): item["chunk_id"] for item in snapshot["requests"]}
        # A request is reused only if the final input, source mapping, prompt and
        # settings agree exactly. Keep its original call ID, timing and usage.
        for call in self.store.list_experiment_calls(self.run_id):
            key = call.get("early_extraction_key")
            if key in keys:
                call["chunk_id"] = keys[key]
                self.store.save_experiment_call(self.run_id, call)
        self.store.finalize_early_experiment(self.run_id, snapshot)
        return self.run_id

    def take(self, snapshot, chunk_id):
        prepared = next(item for item in snapshot["requests"] if item["chunk_id"] == chunk_id)
        key = extraction_key(snapshot, prepared)
        if self.failure is not None and self.failure[0] == key:
            raise self.failure[1]
        return self.completed.get(key)

    def close(self):
        self.cancelled.set()
        with self.condition:
            self.closed = True
            self.latest = None
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        if self.run_id is not None:
            row = self.store.get_experiment(self.run_id)
            if row["status"] == "prefetching":
                self.store.update_experiment(
                    self.run_id, status="interrupted", error_message="ASR 未完成，尚未发布分析结果",
                )


@contextmanager
def early_analysis(judge, store, video_id, *, should_stop=None,
                   force_retranscribe=False, asr_model_id=None, **options):
    start = getattr(judge, "start_early_analysis", None)
    if start is None:
        yield
        return
    model = get_model(asr_model_id or selected_model_id())
    if model.engine != "firered":
        yield
        return
    stream = start(store, video_id, should_stop=should_stop, **options)
    stream.transcription_model = model.id
    stream.allow_resume = not force_retranscribe
    try:
        with transcript_listener(stream.feed):
            yield
    finally:
        try:
            stream.close()
        finally:
            judge._early_extraction = None
