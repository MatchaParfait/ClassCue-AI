from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import sys
import wave
import json

import pytest

import firered_resilience as fr
from core import TranscriptSegment
from processing_control import ProcessingCancelled, ResourcePaused
from transcript_events import transcript_listener


@pytest.mark.parametrize("stream", [False, True])
def test_worker_transcript_notifications_are_optional_and_reach_parent_without_changing_manifest(
    tmp_path, monkeypatch, stream,
):
    import firered_runtime
    import qwen_process

    manifest = {"job_id": "test", "total_samples": 16000, "paths": {}}
    monkeypatch.setattr(firered_runtime, "runtime_python", lambda: Path("worker-python"))
    delivered = []
    def run(_command, **kwargs):
        request = fr.read_json(tmp_path / "request.json")
        assert request["manifest"] == manifest
        assert request.get("stream_transcript", False) is stream
        if stream:
            kwargs["on_line"](json.dumps({
                "event": "transcript", "segments": [asdict(TranscriptSegment(1, 0, 1000, "会考"))],
                "settled_until_ms": 1000,
            }) + "\n")
        return 0
    monkeypatch.setattr(qwen_process, "run_process", run)
    with transcript_listener(
        (lambda added, watermark: delivered.append((added, watermark))) if stream else None,
    ):
        assert fr.run_attempt(tmp_path, manifest, 1, "attempt")["returncode"] == 0
    assert delivered == ([([TranscriptSegment(1, 0, 1000, "会考")], 1000)] if stream else [])


@pytest.mark.parametrize("mib,batch", [
    (24576, 8), (12288, 8), (12224, 8), (12223, 6),
    (8188, 6), (8128, 6), (8127, 5), (6144, 5),
    (6080, 5), (6079, 3), (4096, 3), (4032, 3),
    (4031, 1), (2048, 1), (None, 1), (float("nan"), 1),
])
def test_native_vram_tiers(mib, batch):
    assert fr.select_batch_size(mib) == batch


@pytest.fixture
def job(tmp_path):
    manifest = {"job_id": "fixture", "total_samples": 32000,
                "timeline": str(tmp_path / "timeline.wav"), "paths": {}}
    cp = fr.FireRedCheckpoints(tmp_path, manifest["job_id"], manifest["total_samples"])
    cp.save_vad({"timestamps": [[0, 1], [1, 2]]})
    return tmp_path, manifest, cp


def hardware(batch=8):
    return {"cuda_available": True, "initial_batch_size": batch,
            "gpu_total_mib": {8: 12288, 6: 8188, 5: 6144, 3: 4096, 1: 2048}[batch],
            "device": "cuda:0", "installed_ram_mib": 24576}


def finish(directory, manifest):
    cp = fr.FireRedCheckpoints(directory, manifest["job_id"], manifest["total_samples"])
    vad = cp.load_vad()
    if vad is None:
        cp.save_vad({"timestamps": [[0, 1], [1, 2]]})
        vad = cp.load_vad()
    segments = []
    for index, span in enumerate(vad["timestamps"]):
        chunk = cp.chunk(index, span)
        cp.save(chunk, {"uttid": chunk["uttid"], "text": "hello",
                        "timestamp": [["hello", 0, 1]]})
        segments.append(TranscriptSegment(index + 1, index * 1000, (index + 1) * 1000, "hello", []))
    fr.save_document(Path(directory) / "result.json", manifest["job_id"],
                     segments=[asdict(item) for item in segments], duration_ms=2000)


def test_successful_empty_text_is_distinct_from_missing_or_corrupt_checkpoint(job):
    directory, manifest, cp = job
    chunk = cp.chunk(0, [0, 1])
    assert cp.load(chunk) is None
    cp.save(chunk, {"uttid": chunk["uttid"], "text": "", "timestamp": []})
    assert cp.load(chunk)["text"] == ""
    assert cp.counts() == (1, 2)
    path = directory / "asr" / f"{fr.digest(chunk)}.json"
    value = fr.read_json(path)
    value["output"]["text"] = "tampered"
    fr.atomic_json(path, value)
    assert cp.load(chunk) is None


@pytest.mark.parametrize("text", ["", "<blank>", "<sil>"])
def test_silence_sentinels_have_durable_completion(job, text):
    _, _, cp = job
    chunk = cp.chunk(0, [0, 1])
    cp.save(chunk, {"uttid": chunk["uttid"], "text": text, "timestamp": []})
    assert cp.load(chunk) is not None


def test_full_oom_ladder_and_bounded_pause(job):
    directory, manifest, cp = job
    calls, resources = [], []
    chunk = cp.chunk(0, [0, 1])
    cp.save(chunk, {"uttid": chunk["uttid"], "text": "", "timestamp": []})

    def runner(_directory, _manifest, batch, *_args):
        assert cp.load(chunk) is not None
        calls.append(batch)
        return {"failure": {"kind": "cuda_oom", "phase": "inference", "message": "oom"}}

    with pytest.raises(ResourcePaused):
        fr.run_prepared(directory, manifest, hardware(), attempt_runner=runner, on_resource=resources.append)
    assert calls == [8, 6, 5, 3, 2, 1]
    assert resources[-1]["status"] == "resource_paused"
    assert resources[-1]["completed_segments"] == 1
    assert fr.read_json(directory / "state.json")["current_batch_size"] == 1


@pytest.mark.parametrize("phase", ["load", "vad"])
def test_load_and_vad_oom_retry_same_configuration_once(job, phase):
    directory, manifest, _ = job
    calls = []
    def runner(_directory, _manifest, batch, *_args):
        calls.append(batch)
        return {"failure": {"kind": "cuda_oom", "phase": phase, "message": "oom"}}
    with pytest.raises(ResourcePaused):
        fr.run_prepared(directory, manifest, hardware(6), attempt_runner=runner)
    assert calls == [6, 6]


def test_alignment_oom_uses_same_batch_ladder(job):
    directory, manifest, _ = job
    calls = []
    def runner(directory, manifest, batch, *_args):
        calls.append(batch)
        if len(calls) == 1:
            return {"failure": {"kind": "cuda_oom", "phase": "alignment", "message": "oom"}}
        finish(directory, manifest)
        return {}
    assert fr.run_prepared(directory, manifest, hardware(6), attempt_runner=runner)[1] == 2000
    assert calls == [6, 5]


@pytest.mark.parametrize("kind", ["system_memory", "inference", "environment", "native_exit"])
def test_other_failure_kinds_do_not_blindly_retry(job, kind):
    directory, manifest, _ = job
    calls = []
    def runner(*args):
        calls.append(args[2])
        return {"failure": {"kind": kind, "phase": "inference", "message": "failure"}}
    with pytest.raises(ResourcePaused if kind == "system_memory" else fr.FireRedRunError):
        fr.run_prepared(directory, manifest, hardware(6), attempt_runner=runner)
    assert calls == [6]


def test_success_without_validated_checkpoints_is_failure(job):
    with pytest.raises(fr.FireRedRunError, match="complete validated"):
        fr.run_prepared(job[0], job[1], hardware(6), attempt_runner=lambda *_args: {})


def test_completed_cache_does_not_launch_worker(job):
    directory, manifest, _ = job
    finish(directory, manifest)
    values = []
    assert fr.run_prepared(
        directory, manifest, hardware(6), on_resource=values.append,
        attempt_runner=lambda *_args: pytest.fail("No model load for complete cache"),
    )[1] == 2000
    assert values[-1]["cache_hit"] is True


def test_queue_retains_lower_bs_and_new_queue_resets(job, tmp_path):
    directory, manifest, _ = job
    calls = []
    def runner(directory, manifest, batch, *_args):
        calls.append(batch)
        if len(calls) == 1:
            return {"failure": {"kind": "cuda_oom", "phase": "inference", "message": "oom"}}
        finish(directory, manifest)
        return {}
    other = tmp_path / "other"
    other.mkdir()
    with fr.firered_queue():
        fr.run_prepared(directory, manifest, hardware(6), attempt_runner=runner)
        fr.run_prepared(other, manifest, hardware(6), attempt_runner=runner)
    assert calls == [6, 5, 5]
    (other / "result.json").unlink()
    with fr.firered_queue():
        fr.run_prepared(other, manifest, hardware(6), attempt_runner=runner)
    assert calls[-1] == 6


def test_cancel_persists_interrupted_and_never_launches(job):
    with pytest.raises(ProcessingCancelled):
        fr.run_prepared(job[0], job[1], hardware(6), should_stop=lambda: True,
                        attempt_runner=lambda *_args: pytest.fail("cancelled"))
    assert fr.read_json(job[0] / "state.json")["status"] == "interrupted"


def test_no_cuda_never_decodes_or_launches(tmp_path):
    with pytest.raises(fr.FireRedRunError, match="CUDA"):
        fr.run_firered("absent.mp4", tmp_path, {}, hardware={"cuda_available": False})
    assert list(tmp_path.iterdir()) == []


def test_worker_device_capacity_is_actual_cuda_device_not_smi_gpu_zero():
    from scripts.firered_worker import cuda_hardware
    torch = SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda: True, is_bf16_supported=lambda: True,
        get_device_properties=lambda device: SimpleNamespace(total_memory=6144 * 2**20, name="actual"),
    ))
    actual = cuda_hardware(torch)
    assert actual["gpu_total_mib"] == 6144
    assert fr.select_batch_size(actual["gpu_total_mib"]) == 5


def test_input_and_model_changes_invalidate_identity_but_batch_size_does_not(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"source")
    model = tmp_path / "model"
    model.mkdir()
    weight = model / "weights.bin"
    weight.write_bytes(b"weights")
    paths = {"recognizer": str(model)}
    args = (source, tmp_path / "cache", paths)
    first = fr._prepare_job(*args, hardware(6), None, None)[2]
    assert first == fr._prepare_job(*args, hardware(8), None, None)[2]
    source.write_bytes(b"new source")
    second = fr._prepare_job(*args, hardware(6), None, None)[2]
    assert second != first
    weight.write_bytes(b"new weights")
    assert second != fr._prepare_job(*args, hardware(6), None, None)[2]


def test_recovery_progress_never_moves_backwards(job):
    directory, manifest, _ = job
    calls, fractions = [], []
    def runner(directory, manifest, batch, _attempt, progress, *_args):
        calls.append(batch)
        progress(.04, "loading")
        if len(calls) == 1:
            progress(.6, "saved")
            return {"failure": {"kind": "cuda_oom", "phase": "inference", "message": "oom"}}
        progress(.15, "cached VAD")
        finish(directory, manifest)
        progress(1, "complete")
        return {}
    fr.run_prepared(directory, manifest, hardware(6), attempt_runner=runner,
                    progress=lambda fraction, _message: fractions.append(fraction))
    assert fractions == sorted(fractions)
