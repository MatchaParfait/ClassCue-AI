from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled, ResourcePaused
import classcue.qwen_resilience as qr


@pytest.fixture
def job(tmp_path):
    import numpy as np
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * (270 * 16000))
    options = qr.QwenOptions(mode="manual")
    chunks = qr.make_chunks(np.zeros(270 * 16000, dtype=np.float32), 16000, options, [])
    manifest = {
        "job_id": "job", "identity": {"options": asdict(options)}, "timeline": str(path),
        "rate": 16000, "total_samples": 270 * 16000, "chunks": chunks["chunks"],
        "vad": {"speech_intervals": []},
    }
    return tmp_path, manifest, options


def successful(directory, manifest, chunks, stage, batch, profile, attempt, progress, stop):
    cache = qr.Checkpoints(directory, manifest["job_id"])
    for chunk in chunks:
        if stage == "asr":
            if cache.load("asr", chunk) is None:
                cache.save("asr", chunk, "test")
        else:
            text = cache.load("asr", chunk)
            cache.save("align", chunk, [{
                "id": 1, "start_ms": round(chunk["start_sample"] / 16),
                "end_ms": round(chunk["end_sample"] / 16),
                "text": text, "words": [],
            }], text)
    return {"stage": stage, "batch_size": batch, "attempt_id": attempt}


def failure(kind="cuda_oom", phase="inference"):
    return {"failure": {"kind": kind, "phase": phase, "message": "injected"}}


def test_checkpoints_ignore_corruption_different_job_and_asr(tmp_path):
    chunk = {"start_sample": 0, "end_sample": 16000, "input_samples": 16000,
             "source_chunk_index": "0:16000", "slices": []}
    cache = qr.Checkpoints(tmp_path, "one")
    cache.save("asr", chunk, "text")
    assert cache.load("asr", chunk) == "text"
    assert qr.Checkpoints(tmp_path, "two").load("asr", chunk) is None
    cache.save("align", chunk, [], "text")
    assert cache.load("align", chunk, "changed") is None
    path = tmp_path / "asr" / f"{qr.chunk_key(chunk)}.json"
    payload = qr.read_json(path)
    payload["value"] = "tampered"
    qr.atomic_json(path, payload)
    assert cache.load("asr", chunk) is None
    path.write_text("{", encoding="utf-8")
    assert cache.load("asr", chunk) is None


def test_atomic_write_preserves_old_value_when_serialization_fails(tmp_path):
    path = tmp_path / "data.json"
    qr.atomic_json(path, {"old": 1})
    with pytest.raises(TypeError):
        qr.atomic_json(path, {"new": object()})
    assert qr.read_json(path) == {"old": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_asr_recovery_keeps_successes_and_only_splits_pending(job):
    directory, manifest, options = job
    calls = []
    first = manifest["chunks"][0]

    def runner(*args):
        stage, batch = args[3:5]
        calls.append((stage, batch, max(c["end_sample"] - c["start_sample"] for c in args[2])))
        cache = qr.Checkpoints(directory, manifest["job_id"])
        if len(calls) == 1:
            cache.save("asr", first, "kept")
        if stage == "asr" and len(calls) <= 3:
            return failure()
        return successful(*args)

    segments, duration = qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert [item[:2] for item in calls] == [("asr", 2), ("asr", 1), ("asr", 1),
                                          ("asr", 1), ("align", 2)]
    state = qr.read_json(directory / "state.json")
    assert state["recovery_chunk_seconds"] == 30
    assert state["chunks"][0] == first
    assert segments[0].text == "kept"
    assert qr.valid_chunks(state["chunks"], manifest["total_samples"])
    assert all(c["end_sample"] - c["start_sample"] <= 35 * 16000 for c in state["chunks"][1:])
    assert duration == 270000
    assert len({qr.chunk_key(c) for c in state["chunks"]}) == len(state["chunks"])


def test_cpu_memory_failure_retries_low_memory_once(job):
    directory, manifest, options = job
    calls = []

    def runner(*args):
        calls.append(args[5])
        return failure("system_memory", "load")

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == ["standard", "low-memory"]


@pytest.mark.parametrize("initial_batch", [1, 2])
def test_manual_long_chunks_follow_same_bounded_asr_recovery(job, initial_batch):
    import numpy as np

    directory, manifest, options = job
    options = replace(options, chunk_seconds=180, batch_size=initial_batch)
    manifest["identity"]["options"] = asdict(options)
    manifest["chunks"] = qr.make_chunks(
        np.zeros(manifest["total_samples"], dtype=np.float32), 16000, options, [],
    )["chunks"]
    calls = []

    def runner(*args):
        calls.append((args[4], qr.read_json(directory / "state.json").get(
            "recovery_chunk_seconds", options.chunk_seconds)))
        return failure()

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == [(initial_batch, 180), (1, 90), (1, 60), (1, 30)]


def test_loading_cuda_oom_does_not_pointlessly_shorten_audio(job):
    directory, manifest, options = job
    calls = []

    def runner(*args):
        calls.append((args[4], len(args[2])))
        return failure(phase="load")

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == [(2, len(manifest["chunks"]))] * 2


def test_alignment_pause_resume_does_not_run_asr_again(job):
    directory, manifest, options = job
    calls = []

    def runner(*args):
        calls.append((args[3], args[4]))
        return successful(*args) if args[3] == "asr" else failure()

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == [("asr", 2), ("align", 2), ("align", 1)]
    assert qr.read_json(directory / "state.json")["status"] == "resource_paused"
    resumed = []

    def resume(*args):
        resumed.append(args[3])
        return successful(*args)

    qr.run_prepared(directory, manifest, options, attempt_runner=resume)
    assert resumed == ["align"]
    qr.run_prepared(directory, manifest, options, attempt_runner=lambda *_: pytest.fail("already cached"))


@pytest.mark.parametrize("kind", ["environment", "inference", "native_exit"])
def test_nonresource_failures_are_not_retried(job, kind):
    directory, manifest, options = job
    calls = []

    def runner(*args):
        calls.append(1)
        return failure(kind)

    with pytest.raises(qr.QwenRunError):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == [1]
    assert qr.read_json(directory / "state.json")["status"] == "failed"


def test_manual_can_disable_recovery(job):
    directory, manifest, options = job
    calls = []

    def runner(*args):
        calls.append(1)
        return failure()

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, replace(options, auto_recovery=False), attempt_runner=runner)
    assert calls == [1]


def test_cancellation_preserves_successful_asr(job):
    directory, manifest, options = job
    cache = qr.Checkpoints(directory, manifest["job_id"])
    first = manifest["chunks"][0]
    cache.save("asr", first, "done")
    with pytest.raises(ProcessingCancelled):
        qr.run_prepared(directory, manifest, options, should_stop=lambda: True)
    assert cache.load("asr", first) == "done"
    assert qr.read_json(directory / "state.json")["status"] == "interrupted"


@pytest.mark.parametrize("gpu,ram,batch,seconds,profile", [
    (8188, 16384, 2, 90, "standard"),
    (8192, 32768, 2, 90, "standard"),
    (24576, 65536, 2, 90, "standard"),
    (6140, 12288, 2, 60, "standard"),
    (6144, 16384, 2, 60, "standard"),
    (8192, 12288, 2, 60, "standard"),
    (8192, 8192, 1, 30, "low-memory"),
    (6144, 8192, 1, 30, "low-memory"),
    (4096, 32768, 1, 30, "low-memory"),
    (None, 16384, 1, 30, "low-memory"),
    (8192, None, 1, 30, "low-memory"),
    (float("nan"), 16384, 1, 30, "low-memory"),
])
def test_auto_routes_by_native_capacity_and_caps_high_end(monkeypatch, gpu, ram, batch, seconds, profile):
    monkeypatch.setattr(qr, "native_hardware",
                        lambda: {"gpu_total_mib": gpu, "installed_ram_mib": ram})
    monkeypatch.setattr(qr, "gpu_free_mib", lambda: pytest.fail("free memory is not a routing input"))
    configured = qr.QwenOptions().configured()
    assert (configured.batch_size, configured.align_batch_size, configured.chunk_seconds,
            configured.load_profile) == (batch, batch, seconds, profile)


def test_auto_no_longer_downshifts_just_because_other_apps_use_vram(job, monkeypatch):
    directory, manifest, _ = job
    monkeypatch.setattr(qr, "native_hardware",
                        lambda: {"gpu_total_mib": 8188, "installed_ram_mib": 24576})
    monkeypatch.setattr(qr, "gpu_free_mib", lambda: 1000)
    calls = []

    def runner(*args):
        calls.append((args[3], args[4]))
        return successful(*args)

    qr.run_prepared(directory, manifest, qr.QwenOptions().configured(), attempt_runner=runner)
    assert calls[0] == ("asr", 2)


def test_sixty_second_tier_has_no_ninety_second_recovery_attempt(job):
    import numpy as np

    directory, manifest, options = job
    options = replace(options, chunk_seconds=60)
    manifest["identity"]["options"] = asdict(options)
    manifest["chunks"] = qr.make_chunks(
        np.zeros(manifest["total_samples"], dtype=np.float32), 16000, options, [],
    )["chunks"]
    calls = []

    def runner(*args):
        calls.append((args[4], qr.read_json(directory / "state.json").get(
            "recovery_chunk_seconds", options.chunk_seconds)))
        return failure()

    with pytest.raises(ResourcePaused):
        qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == [(2, 60), (1, 60), (1, 30)]


def test_queue_retains_recovery_only_until_next_queue(job):
    directory, manifest, options = job
    first_calls = []

    def first(*args):
        stage, batch = args[3:5]
        first_calls.append((stage, batch))
        if stage == "asr" and len(first_calls) <= 3:
            return failure()
        if stage == "align" and batch == 2:
            return failure()
        return successful(*args)

    with qr.qwen_queue() as queue:
        qr.run_prepared(directory, manifest, options, attempt_runner=first)
        assert (queue.asr_batch_size, queue.chunk_seconds, queue.align_batch_size) == (1, 30, 1)
        following_calls = []

        def following(*args):
            following_calls.append((args[3], args[4]))
            return successful(*args)

        second_directory = directory / "second"
        qr.run_prepared(second_directory, manifest, options, attempt_runner=following)
        assert following_calls == [("asr", 1), ("align", 1)]
        state = qr.read_json(second_directory / "state.json")
        assert state["recovery_chunk_seconds"] == 30
        assert qr.valid_chunks(state["chunks"], manifest["total_samples"])
    with qr.qwen_queue() as queue:
        fresh_calls = []

        def fresh(*args):
            fresh_calls.append((args[3], args[4]))
            return successful(*args)

        third_directory = directory / "third"
        qr.run_prepared(third_directory, manifest, options, attempt_runner=fresh)
        assert fresh_calls == [("asr", 2), ("align", 2)]
        assert queue.chunk_seconds is None
        assert qr.read_json(third_directory / "state.json")["chunks"] == manifest["chunks"]


def test_queue_keeps_low_memory_loading_but_new_queue_resets(job):
    directory, manifest, options = job
    calls = []

    def first(*args):
        calls.append((args[3], args[5]))
        return failure("system_memory", "load") if len(calls) == 1 else successful(*args)

    with qr.qwen_queue() as queue:
        qr.run_prepared(directory, manifest, options, attempt_runner=first)
        assert queue.low_memory
        profiles = []

        def subsequent(*args):
            profiles.append(args[5])
            return successful(*args)

        qr.run_prepared(directory / "second", manifest, options, attempt_runner=subsequent)
        assert profiles == ["low-memory", "low-memory"]
    with qr.qwen_queue() as queue:
        assert not queue.low_memory


def test_queue_hardware_snapshot_is_per_start_not_per_file(job, monkeypatch):
    directory, manifest, _ = job
    snapshots = []

    def hardware():
        snapshots.append(1)
        return {"gpu_total_mib": 8188, "installed_ram_mib": 24576}

    def prepare(_input, root, _paths, options, *_args):
        assert options.chunk_seconds == 90
        return Path(root) / "job", manifest

    monkeypatch.setattr(qr, "native_hardware", hardware)
    monkeypatch.setattr(qr, "prepare_job", prepare)
    with qr.qwen_queue():
        qr.run_qwen("unused.mp4", directory / "one", {}, attempt_runner=successful)
        qr.run_qwen("unused.mp4", directory / "two", {}, attempt_runner=successful)
    assert len(snapshots) == 1
    with qr.qwen_queue():
        qr.run_qwen("unused.mp4", directory / "three", {}, attempt_runner=successful)
    assert len(snapshots) == 2


def test_load_profile_and_modes_validate():
    assert qr.QwenOptions(mode="low-resource").configured().chunk_seconds == 30
    assert qr.QwenOptions(mode="low-resource").configured().load_profile == "low-memory"
    with pytest.raises(ValueError):
        qr.QwenOptions(chunk_seconds=0)
    with pytest.raises(ValueError):
        qr.QwenOptions(mode="other")
    with pytest.raises(ValueError):
        qr.QwenOptions(batch_size=1.5)


def test_classify_errors_does_not_treat_every_runtime_error_as_oom():
    class CudaOOM(RuntimeError):
        pass

    torch = SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=CudaOOM))
    assert qr.classify_error(CudaOOM(), torch) == "cuda_oom"
    assert qr.classify_error(MemoryError()) == "system_memory"
    assert qr.classify_error(RuntimeError("DefaultCPUAllocator: can't allocate memory: 10")) == "system_memory"
    assert qr.classify_error(RuntimeError("CUDA out of memory string from another component")) == "inference"
    assert qr.classify_error(ImportError()) == "environment"


def test_invalid_timestamps_are_rejected():
    with pytest.raises(ValueError):
        qr.validate_segments([TranscriptSegment(1, 1000, 500, "text", [])], 2000)


def test_checkpointed_empty_asr_is_not_missing(job):
    directory, manifest, options = job
    cache = qr.Checkpoints(directory, manifest["job_id"])
    for chunk in manifest["chunks"]:
        cache.save("asr", chunk, "")
    calls = []

    def runner(*args):
        calls.append(args[3])
        return successful(*args)

    qr.run_prepared(directory, manifest, options, attempt_runner=runner)
    assert calls == ["align"]


def test_owned_process_cancellation_reaps_venv_children(tmp_path):
    import sys
    import time
    from classcue.qwen_process import run_process

    started = time.monotonic()
    with pytest.raises(ProcessingCancelled):
        run_process([sys.executable, "-u", "-c",
                     "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                     "print('ready',flush=True); time.sleep(60)"],
                    should_stop=lambda: time.monotonic() - started > 1)
    assert time.monotonic() - started < 10


def test_native_parent_exit_reaps_child_holding_output_pipe():
    import sys
    import time
    from classcue.qwen_process import run_process

    started = time.monotonic()
    code = run_process([
        sys.executable, "-u", "-c",
        "import subprocess,sys,os;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print('child started',flush=True);os._exit(7)",
    ], timeout=10)
    assert code == 7
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("phase", ["load", "inference"])
@pytest.mark.parametrize("stream_preview", [False, True])
def test_worker_reports_typed_cuda_oom_and_keeps_committed_batches(job, monkeypatch, capsys, phase, stream_preview):
    import sys
    from contextlib import nullcontext
    import classcue.qwen_transcriber as qwen_transcriber
    from scripts import qwen_resilient_worker

    directory, manifest, options = job
    manifest["paths"] = {"recognizer": "recognizer", "aligner": "aligner"}
    class CudaOOM(RuntimeError):
        pass

    class Model:
        calls = 0

        def transcribe(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise CudaOOM("injected")
            return [SimpleNamespace(text="committed") for _ in kwargs["audio"]]

    def load(*args, **kwargs):
        if phase == "load":
            raise CudaOOM("injected")
        return Model()

    cuda = SimpleNamespace(
        OutOfMemoryError=CudaOOM, is_available=lambda: True, synchronize=lambda: None,
        get_device_name=lambda _: "fake", reset_peak_memory_stats=lambda: None,
        empty_cache=lambda: None, memory_allocated=lambda: 0, memory_reserved=lambda: 0,
        max_memory_allocated=lambda: 0, max_memory_reserved=lambda: 0,
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda, set_num_threads=lambda _: None))
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(Qwen3ForcedAligner=object))
    monkeypatch.setattr(qwen_transcriber, "load_qwen_model", load)
    monkeypatch.setattr(qwen_transcriber, "_checkpoint_reader", nullcontext)
    monkeypatch.setattr(qr, "gpu_free_mib", lambda: 7000)
    request_path = directory / "request.json"
    qr.atomic_json(request_path, {"manifest": manifest, "stage": "asr", "batch_size": 2,
                                "stream_preview": stream_preview,
                                "chunks": manifest["chunks"], "load_profile": "standard", "attempt_id": "test"})
    monkeypatch.setattr(sys, "argv", ["worker", "--request", str(request_path), "--output", str(directory)])
    assert qwen_resilient_worker.main() == 1
    report = qr.read_json(directory / "failure-test.json")
    assert report["kind"] == "cuda_oom"
    assert report["phase"] == phase
    cache = qr.Checkpoints(directory, manifest["job_id"])
    assert (cache.load("asr", manifest["chunks"][0]) == "committed") is (phase == "inference")
    previews = [event for line in capsys.readouterr().out.splitlines()
                if (event := qr.read_event(line)) and event.get("event") == "transcript_preview"]
    assert bool(previews) is (stream_preview and phase == "inference")
    if previews:
        assert len(previews) == 1
        assert len(previews[0]["segments"]) == 2
        assert previews[0]["segments"][0]["text"] == "committed"
        assert previews[0]["segments"][0]["id"] == -1


def test_preparation_memory_error_pauses_instead_of_running_model(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "prepare_job",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryError("injected")))
    with pytest.raises(ResourcePaused) as result:
        qr.run_qwen(tmp_path / "video.mp4", tmp_path / "cache", {})
    assert result.value.failure["phase"] == "preparation"


def test_subprocess_exit_without_failure_report_is_native_exit(tmp_path, job, monkeypatch):
    import classcue.qwen_process as qwen_process
    import classcue.qwen_runtime as qwen_runtime
    directory, manifest, options = job
    monkeypatch.setattr(qwen_process, "run_process", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(qwen_runtime, "runtime_python", lambda: "python")
    record = qr.run_attempt(directory, manifest, manifest["chunks"], "asr", 2, "standard", "injected")
    assert record["failure"]["kind"] == "native_exit"
    assert record["started_at"] <= record["exited_at"]
