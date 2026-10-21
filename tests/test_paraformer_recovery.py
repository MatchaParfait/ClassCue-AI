import io
import json
from queue import Queue
from types import SimpleNamespace
import wave

import pytest

import classcue.paraformer_transcriber as paraformer
from classcue.processing_control import ResourcePaused
from classcue.transcriber import ParaformerWorker


def validate_output(segments, duration_ms, audit):
    expected = audit["batch_size"]
    targets = [batch.get("target_batch_size", expected) for batch in audit.get("batches", [])]
    sizes = audit["batch_sizes"]
    if not targets:
        targets = [expected] * len(sizes)
    assert len(targets) == len(sizes)
    assert all(1 <= target <= expected for target in targets)
    assert sizes[:-1] == targets[:-1]
    assert not sizes or 1 <= sizes[-1] <= targets[-1]
    if "input_order" in audit:
        order = audit["input_order"]
        assert sorted(order) == list(range(len(audit["speech_intervals_ms"])))
        assert [index for batch in audit["batches"] for index in batch["original_indices"]] == order
    last_start = -1
    for index, segment in enumerate(segments, 1):
        assert segment.id == index
        assert last_start <= segment.start_ms < segment.end_ms <= duration_ms + 1
        assert segment.start_ms >= 0
        assert segment.words
        for word in segment.words:
            assert word["start"] * 1000 >= segment.start_ms - 2
            assert word["end"] * 1000 <= segment.end_ms + 2
        last_start = segment.start_ms
    assert not audit["speech_intervals_ms"] or segments
    return {"subtitle_count": len(segments)}


class CudaOOM(RuntimeError):
    pass


@pytest.fixture
def recovery_setup(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(OutOfMemoryError=CudaOOM),
    ))
    monkeypatch.setattr(paraformer, "synchronize_device", lambda _: None)
    cleaned = []
    monkeypatch.setattr(paraformer, "release_cuda_memory", lambda: cleaned.append(True))

    def decode(path, destination, progress):
        with wave.open(str(destination), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b"\0\0" * (42 * 16000))
        return 42000

    intervals = [(i * 1000, i * 1000 + 800) for i in range(40)]
    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(paraformer, "detect_speech", lambda *_a, **_kw: intervals)
    return tmp_path, cleaned


def output_for(audios):
    return [{"text": "你", "timestamp": [[0, 100]]} for _ in audios]


def test_only_failed_batch_retries_and_reduced_batch_is_retained(recovery_setup):
    directory, cleaned = recovery_setup
    attempts = []

    def generate(**kwargs):
        attempts.append(len(kwargs["input"]))
        if len(attempts) == 2:
            raise CudaOOM("test GPU allocation failure")
        return output_for(kwargs["input"])

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16, "gpu")
    audit = {}
    segments, duration = paraformer.transcribe_paraformer(
        model, "video.mp4", batch_size=16, temporary_directory=directory, audit=audit,
    )
    assert attempts == [16, 16, 8, 8, 8]
    assert audit["batch_sizes"] == [16, 8, 8, 8]
    assert audit["effective_batch_size"] == 8
    assert audit["oom_recovery"][0]["completed_intervals"] == 16
    assert [i for batch in audit["batches"] for i in batch["original_indices"]] == list(range(40))
    assert validate_output(segments, duration, audit)["subtitle_count"] == 40
    assert len(cleaned) == 1


def test_finite_ladder_to_one_and_new_task_resets(recovery_setup):
    directory, cleaned = recovery_setup
    attempts = []

    def generate(**kwargs):
        size = len(kwargs["input"])
        attempts.append(size)
        if size > 1:
            raise CudaOOM("test GPU allocation failure")
        return output_for(kwargs["input"])

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16, "gpu")
    for _ in range(2):
        audit = {}
        segments, duration = paraformer.transcribe_paraformer(
            model, "video.mp4", batch_size=16, temporary_directory=directory, audit=audit,
        )
        assert [item["next_batch_size"] for item in audit["oom_recovery"]] == [8, 4, 2, 1]
        assert audit["batch_sizes"] == [1] * 40
        assert validate_output(segments, duration, audit)["subtitle_count"] == 40
    assert attempts[:4] == [16, 8, 4, 2]
    assert attempts[44:48] == [16, 8, 4, 2]
    assert len(cleaned) == 8


def test_bs1_failure_pauses_without_unbounded_retry(recovery_setup):
    directory, cleaned = recovery_setup
    attempts = []

    def generate(**kwargs):
        attempts.append(len(kwargs["input"]))
        raise CudaOOM("test GPU allocation failure")

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16, "gpu")
    audit = {}
    with pytest.raises(ResourcePaused) as paused:
        paraformer.transcribe_paraformer(
            model, "video.mp4", batch_size=16, temporary_directory=directory, audit=audit,
        )
    assert attempts == [16, 8, 4, 2, 1]
    assert paused.value.failure["next_batch_size"] is None
    assert audit["batch_sizes"] == []
    assert len(cleaned) == 5


@pytest.mark.parametrize("device,error", [
    ("gpu", RuntimeError("ordinary error, not an allocation failure")),
    ("cpu", CudaOOM("not a GPU task")),
    ("gpu", MemoryError("host RAM failure")),
])
def test_non_cuda_errors_and_cpu_path_never_retry(recovery_setup, device, error):
    directory, cleaned = recovery_setup
    attempts = []

    def generate(**kwargs):
        attempts.append(True)
        raise error

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16, device)
    with pytest.raises(type(error)):
        paraformer.transcribe_paraformer(
            model, "video.mp4", temporary_directory=directory,
        )
    assert len(attempts) == 1
    assert cleaned == []


def test_worker_resource_pause_reaches_application(tmp_path):
    worker = ParaformerWorker.__new__(ParaformerWorker)
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({
        "event": "resource_paused", "message": "BS1 still OOM",
        "failure": {"stage": "asr", "batch_size": 1},
    }))
    closed = []
    worker.close = lambda **_kwargs: closed.append(True)
    with pytest.raises(ResourcePaused) as paused:
        worker.transcribe("video.mp4", {"recognizer": tmp_path, "vad": tmp_path}, None)
    assert paused.value.failure == {"stage": "asr", "batch_size": 1}
    assert closed == [True]
