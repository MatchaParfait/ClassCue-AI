from types import SimpleNamespace
import io
import json
from pathlib import Path
import sys
from queue import Queue
import sqlite3

import pytest

import classcue.asr_models as asr_models
from classcue.core import SemanticSelection, TranscriptSegment
import classcue.transcriber as transcriber
import classcue.video_pipeline as video_pipeline
from classcue.video_store import VideoStore


@pytest.fixture
def video(tmp_path):
    store = VideoStore(tmp_path / "course.db")
    path = tmp_path / "lesson.mp4"
    path.write_bytes(b"media")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 5000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "旧字幕")], "whisper-turbo")
    marker_id = store.add_marker(video_id, 0, 1000, "旧重点", "", "旧字幕", "AI-confirmed", .8)
    store.set_marker_review_status(marker_id, "mastered")
    store.set_analysis_run(video_id, "high_recall", "completed", 1)
    store.save_ai_chunk_selection(video_id, "high_recall", "chunk", '{"candidates":[]}')
    store.set_status(video_id, "review_ready")
    return store, video_id


def test_retranscription_replaces_results_and_records_model_after_success(video, monkeypatch):
    store, video_id = video
    calls = []

    def transcribe(_video, _audio, progress, model_id, **kwargs):
        calls.append(model_id)
        assert store.get_segments(video_id)[0].text == "旧字幕"
        return [TranscriptSegment(1, 100, 2000, "单纯形法")], 5000

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    assert video_pipeline.retranscribe_video(store, video_id, "qwen3-asr-1.7b") == 1
    assert calls == ["qwen3-asr-1.7b"]
    assert store.get_video(video_id)["transcription_model"] == "qwen3-asr-1.7b"
    assert store.get_video(video_id)["status"] == "transcribed"
    assert store.get_segments(video_id)[0].text == "单纯形法"
    assert store.list_markers(video_id) == []
    assert store.get_analysis_run(video_id, "high_recall") is None
    assert store.get_ai_chunk_selection(video_id, "high_recall", "chunk") is None


def test_failed_retranscription_keeps_old_subtitles_markers_and_status(video, monkeypatch):
    store, video_id = video
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ASR failed")))
    with pytest.raises(RuntimeError):
        video_pipeline.retranscribe_video(store, video_id, "qwen3-asr-1.7b")
    assert store.get_video(video_id)["status"] == "review_ready"
    assert store.get_video(video_id)["transcription_model"] == "whisper-turbo"
    assert store.get_segments(video_id)[0].text == "旧字幕"
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"
    assert store.get_analysis_run(video_id, "high_recall")["status"] == "completed"


def test_busy_video_cannot_be_retranscribed(video, monkeypatch):
    store, video_id = video
    store.set_status(video_id, "processing")
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args: pytest.fail("Busy video must not transcribe"))
    with pytest.raises(ValueError):
        video_pipeline.retranscribe_video(store, video_id, "whisper-large-v3")


def test_empty_retranscription_keeps_existing_results(video, monkeypatch):
    store, video_id = video
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args, **_kwargs: ([], 5000))
    with pytest.raises(ValueError):
        video_pipeline.retranscribe_video(store, video_id, "qwen3-asr-1.7b")
    assert store.get_video(video_id)["status"] == "review_ready"
    assert store.get_segments(video_id)[0].text == "旧字幕"
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"


def test_forced_processing_retranscribes_a_review_ready_video(video, monkeypatch):
    store, video_id = video
    calls = []

    def transcribe(*_args, **kwargs):
        calls.append(_args[3])
        return [TranscriptSegment(1, 100, 2000, "课程重点")], 5000

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    judge = SimpleNamespace(judge_semantic_chunk=lambda _chunk: SemanticSelection())
    assert video_pipeline.process_video_once(
        store, video_id, judge, asr_model_id="fireredasr2-aed", force_retranscribe=True,
    ) == ("completed", 0)
    assert calls == ["fireredasr2-aed"]
    assert store.get_video(video_id)["transcription_model"] == "fireredasr2-aed"
    assert store.get_segments(video_id)[0].text == "课程重点"
    assert store.get_video(video_id)["status"] == "review_ready"


def test_default_processing_does_not_replace_review_results(video, monkeypatch):
    store, video_id = video
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args, **_kwargs: pytest.fail("Must explicitly request replacement"))
    assert video_pipeline.process_video_once(
        store, video_id, object(), asr_model_id="fireredasr2-aed",
    ) == ("skipped", 1)
    assert store.get_video(video_id)["transcription_model"] == "whisper-turbo"


def test_cancelled_retranscription_preserves_previous_results(video, monkeypatch):
    from classcue.processing_control import ProcessingCancelled

    store, video_id = video
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args, **_kwargs: ([TranscriptSegment(1, 0, 2000, "新字幕")], 5000))
    with pytest.raises(ProcessingCancelled):
        video_pipeline.retranscribe_video(
            store, video_id, "fireredasr2-aed", should_stop=lambda: True,
        )
    assert store.get_video(video_id)["status"] == "review_ready"
    assert store.get_segments(video_id)[0].text == "旧字幕"
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"


def test_failed_database_replace_rolls_back_old_subtitles_and_markers(video):
    store, video_id = video
    duplicate_segments = [TranscriptSegment(1, 0, 1000, "新字幕"), TranscriptSegment(1, 1000, 2000, "重复编号")]
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_transcript_for_reanalysis(video_id, duplicate_segments, "qwen3-asr-1.7b")
    assert store.get_video(video_id)["transcription_model"] == "whisper-turbo"
    assert store.get_segments(video_id)[0].text == "旧字幕"
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"


def test_media_fallback_receives_same_selected_model(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"media")
    calls = []

    def transcribe(path, **kwargs):
        calls.append(kwargs["model_id"])
        if str(path).endswith(".mp4"):
            raise RuntimeError("decode failed")
        return [], 1000

    monkeypatch.setattr(video_pipeline, "transcribe_audio", transcribe)
    monkeypatch.setattr(video_pipeline, "extract_audio", lambda _path, audio: audio.write_bytes(b"wav"))
    video_pipeline._transcribe_video_with_fallback(
        path, tmp_path / "audio.wav", asr_model_id="whisper-large-v3",
    )
    assert calls == ["whisper-large-v3", "whisper-large-v3"]


def test_setup_failure_does_not_retry_or_extract_wav(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"media")
    monkeypatch.setattr(video_pipeline, "transcribe_audio",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(transcriber.ASRSetupError("bad setup")))
    monkeypatch.setattr(video_pipeline, "extract_audio", lambda *_args: pytest.fail("No WAV retry"))
    with pytest.raises(transcriber.ASRSetupError):
        video_pipeline._transcribe_video_with_fallback(path, tmp_path / "audio.wav")


def test_whisper_is_loaded_from_managed_local_directory_and_reused(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    transcriber.unload_model()
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    ))
    first = transcriber._load_whisper_model("whisper-turbo", "cpu", "int8")
    assert transcriber._load_whisper_model("whisper-turbo", "cpu", "int8") is first
    assert len(calls) == 1
    assert calls[0][1]["local_files_only"] is True
    assert "whisper-turbo" in calls[0][0][0]
    assert transcriber._load_whisper_model("whisper-large-v3", "cpu", "int8") is not first
    transcriber.unload_model()


def test_qwen_worker_protocol_maps_progress_and_segment_results(tmp_path):
    worker = transcriber.QwenWorker.__new__(transcriber.QwenWorker)
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({"event": "progress", "fraction": .5, "message": "对齐中"}))
    worker.events.put(json.dumps({"event": "result", "duration_ms": 2000, "segments": [
        {"id": 1, "start_ms": 100, "end_ms": 1500, "text": "考点", "words": []},
    ]}))
    progress = []
    segments, duration = worker.transcribe(
        "video.mp4", {"recognizer": tmp_path / "asr", "aligner": tmp_path / "aligner"},
        lambda value, message: progress.append((value, message)),
    )
    assert duration == 2000
    assert segments[0] == TranscriptSegment(1, 100, 1500, "考点", [])
    assert progress == [(.5, "对齐中")]
    request = json.loads(worker.process.stdin.getvalue())
    assert request["recognizer"] == str(tmp_path / "asr")
    assert request["aligner"] == str(tmp_path / "aligner")


@pytest.mark.parametrize("event", [None, json.dumps({"event": "error", "message": "CUDA out of memory"})])
def test_qwen_worker_failures_are_actionable_setup_errors(tmp_path, event):
    worker = transcriber.QwenWorker.__new__(transcriber.QwenWorker)
    worker.process = SimpleNamespace(stdin=io.StringIO(), poll=lambda: 1)
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(event)
    with pytest.raises(transcriber.ASRSetupError, match="worker.log"):
        worker.transcribe("video.mp4", {"recognizer": tmp_path, "aligner": tmp_path}, None)


def test_interrupted_qwen_transcription_unloads_worker_before_next_request(tmp_path, monkeypatch):
    class Cancelled(BaseException):
        pass

    closed = []

    class Worker:
        def transcribe(self, *_args):
            raise Cancelled()

        def close(self):
            closed.append(True)

    transcriber.unload_model()
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_args: None)
    import classcue.qwen_resilience as qwen_resilience
    monkeypatch.setattr(transcriber, "QwenWorker", Worker)
    transcriber._loaded_model = Worker()
    transcriber._loaded_key = ("qwen3-asr-1.7b", "legacy")
    monkeypatch.setattr(qwen_resilience, "run_qwen",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(Cancelled()))
    with pytest.raises(Cancelled):
        transcriber.transcribe_audio(tmp_path / "audio.wav", model_id="qwen3-asr-1.7b")
    assert closed == [True]
    assert transcriber._loaded_model is None
    assert transcriber._loaded_key is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv launcher process tree")
def test_windows_worker_close_terminates_the_owned_interpreter_tree(monkeypatch):
    calls = []
    running = [True]
    worker = transcriber.QwenWorker.__new__(transcriber.QwenWorker)
    worker.process = SimpleNamespace(
        pid=12345, poll=lambda: None if running[0] else 1,
        stdin=io.StringIO(), stdout=io.StringIO(),
        terminate=lambda: pytest.fail("The complete tree should already be stopped"),
    )
    worker.reader = SimpleNamespace(join=lambda **_kwargs: None)

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 15
        running[0] = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transcriber.subprocess, "run", run)
    worker.close()
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
    assert worker.process.stdin.closed
    assert worker.process.stdout.closed


@pytest.mark.parametrize("device", ["gpu", "cpu"])
def test_firered_worker_protocol_sends_vad_directory_and_maps_native_progress(tmp_path, device):
    worker = transcriber.FireRedWorker.__new__(transcriber.FireRedWorker)
    worker.device = device
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({"event": "progress", "fraction": .15, "message": "VAD ready"}))
    worker.events.put(json.dumps({"event": "result", "duration_ms": 2000, "segments": [
        {"id": 1, "start_ms": 100, "end_ms": 1500, "text": "speech", "words": []},
    ]}))
    progress = []
    _, duration = worker.transcribe(
        "video.mp4", {"recognizer": tmp_path / "asr", "vad": tmp_path / "vad"},
        lambda fraction, message: progress.append((fraction, message)),
    )
    request = json.loads(worker.process.stdin.getvalue())
    assert request["vad"] == str(tmp_path / "vad" / "VAD")
    assert request["device"] == device
    assert not Path(request["temporary_directory"]).exists()
    assert duration == 2000
    assert progress == [(.15, "VAD ready")]


@pytest.mark.parametrize("device", ["gpu", "cpu"])
def test_firered_worker_launch_sets_device_and_cpu_hides_cuda(tmp_path, monkeypatch, device):
    calls = []
    monkeypatch.setattr(transcriber, "ROOT", tmp_path)
    monkeypatch.setattr(transcriber, "firered_runtime_is_ready", lambda: True)
    monkeypatch.setattr(transcriber, "firered_runtime_python", lambda: tmp_path / "python.exe")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=io.StringIO(), stdin=io.StringIO(), poll=lambda: 0)

    monkeypatch.setattr(transcriber.subprocess, "Popen", spawn)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = transcriber.FireRedWorker(device=device)
    worker.reader.join(timeout=2)
    assert calls[0][0][-2:] == ["--device", device]
    if device == "cpu":
        assert calls[0][1]["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
    else:
        assert "CUDA_VISIBLE_DEVICES" not in calls[0][1]["env"]
    worker.close()


def test_firered_cpu_is_blocked_before_preparation_without_gpu_fallback(tmp_path, monkeypatch):
    import classcue.firered_resilience as firered_resilience

    monkeypatch.setattr(transcriber, "FireRedWorker", lambda *_a, **_k: pytest.fail("No CPU worker"))
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_: pytest.fail("No preparation"))
    monkeypatch.setattr(transcriber, "selected_firered_device", lambda: "cpu")
    monkeypatch.setattr(firered_resilience, "run_firered",
                        lambda *_a, **_kw: pytest.fail("No silent GPU fallback"))
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    with pytest.raises(transcriber.ASRSetupError, match="CPU.*禁用"):
        transcriber.transcribe_audio("video.mp4", model_id="firered")


def test_legacy_firered_cpu_preparation_cannot_install_or_download(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "selected_firered_device", lambda: "cpu")
    monkeypatch.setattr(transcriber, "install_firered_runtime", lambda *_: pytest.fail("No installation"))
    monkeypatch.setattr(transcriber, "download_model", lambda *_: pytest.fail("No download"))
    with pytest.raises(ValueError, match="CPU.*禁用"):
        transcriber.prepare_asr_model("firered")


def test_firered_orchestration_receives_native_vad_and_workspace_cache(tmp_path, monkeypatch):
    import classcue.firered_resilience as firered_resilience
    paths = {"recognizer": tmp_path / "asr", "vad": tmp_path / "vad"}
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_args: None)
    monkeypatch.setattr(transcriber, "component_paths", lambda _id: paths)
    monkeypatch.setattr(transcriber, "selected_firered_device", lambda: "gpu")
    calls = []
    monkeypatch.setattr(firered_resilience, "run_firered",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or ([], 2000))
    cache = tmp_path / "workspace" / "firered_jobs"
    assert transcriber.transcribe_audio("video.mp4", model_id="fireredasr2-aed",
                                       cache_directory=cache) == ([], 2000)
    assert calls[0][0][1] == cache
    assert calls[0][0][2]["vad"] == str(paths["vad"] / "VAD")


def test_firered_cancel_is_not_wrapped_as_setup_or_media_error(tmp_path, monkeypatch):
    from classcue.processing_control import ProcessingCancelled
    import classcue.firered_resilience as firered_resilience
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_args: None)
    monkeypatch.setattr(transcriber, "selected_firered_device", lambda: "gpu")
    monkeypatch.setattr(transcriber, "component_paths", lambda _id: {
        "recognizer": tmp_path, "vad": tmp_path,
    })
    monkeypatch.setattr(firered_resilience, "run_firered",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessingCancelled("stop")))
    with pytest.raises(ProcessingCancelled):
        transcriber.transcribe_audio("video.mp4", model_id="fireredasr2-aed")


def test_firered_cpu_cancel_during_vad_closes_worker(tmp_path):
    from classcue.processing_control import ProcessingCancelled

    worker = transcriber.FireRedWorker.__new__(transcriber.FireRedWorker)
    worker.device = "cpu"
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({"event": "progress", "fraction": .05, "message": "VAD"}))
    closed = []
    worker.close = lambda **_kwargs: closed.append(True)
    checks = iter([False, True])
    with pytest.raises(ProcessingCancelled):
        worker.transcribe("video.mp4", {"recognizer": tmp_path, "vad": tmp_path},
                          None, lambda: next(checks))
    assert closed == [True]
