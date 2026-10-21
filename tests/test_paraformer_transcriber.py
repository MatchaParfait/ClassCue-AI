import io
import json
from pathlib import Path
from queue import Queue
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from core import TranscriptSegment
import paraformer_transcriber as paraformer
import transcriber


def test_official_timestamp_units_map_to_absolute_word_and_subtitle_times():
    result = paraformer.timestamp_segments({
        "text": "线 性 KKT 条 件",
        "timestamp": [[100, 200], [200, 400], [500, 1000], [1100, 1300], [1300, 1500]],
    }, 60000, 3000, 8)
    assert result[0].id == 8
    assert result[0].text == "线性KKT条件"
    assert (result[0].start_ms, result[0].end_ms) == (60100, 61500)
    assert result[0].words[2] == {
        "word": "KKT", "start": 60.5, "end": 61.0, "probability": None,
    }


@pytest.mark.parametrize("output", [
    {"text": "你 好", "timestamp": []},
    {"text": "你 好", "timestamp": [[0, 100]]},
    {"text": "你", "timestamp": [[float("nan"), 100]]},
    {"text": "你", "timestamp": [[100, 0]]},
    {"text": "你", "timestamp": [[0, 9000]]},
    {"text": "", "timestamp": [[0, 100]]},
    {"text": "你 好", "timestamp": [[500, 600], [100, 200]]},
])
def test_invalid_alignment_never_falls_back_to_fabricated_vad_timestamps(output):
    with pytest.raises(ValueError):
        paraformer.timestamp_segments(output, 0, 2000)


def test_english_token_whitespace_is_preserved():
    result = paraformer.timestamp_segments({
        "text": "hello world 中 文",
        "timestamp": [[0, 100], [100, 200], [200, 300], [300, 400]],
    }, 0, 1000)
    assert result[0].text == "hello world中文"


def test_timestamp_at_audio_end_and_zero_length_unit_keeps_valid_cue():
    result = paraformer.timestamp_segments({"text": "好", "timestamp": [[1000, 1000]]}, 9000, 1000)
    assert result[0].start_ms == 10000
    assert result[0].end_ms == 10001
    assert result[0].words[0]["start"] == result[0].words[0]["end"] == 10.0


def test_encoder_observer_records_true_tensor_batch_and_removes_hook_on_failure():
    hooks, removed = [], []

    def register(callback):
        hooks.append(callback)
        return SimpleNamespace(remove=lambda: removed.append(True))

    recognizer = SimpleNamespace(model=SimpleNamespace(encoder=SimpleNamespace(
        register_forward_pre_hook=register,
    )))
    with pytest.raises(RuntimeError):
        with paraformer.observe_encoder_batches(recognizer) as sizes:
            hooks[0](None, (np.zeros((4, 20, 560)),))
            assert sizes == [4]
            raise RuntimeError("failed inference")
    assert removed == [True]


def test_cpu_loader_disables_cuda_and_does_not_request_online_or_extra_models(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(paraformer, "_validate_component", lambda path, kind: Path(path))
    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        set_num_threads=lambda value: calls.append(("threads", value)),
        set_num_interop_threads=lambda value: calls.append(("interop", value)),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)

    def factory(**options):
        calls.append(options)
        return SimpleNamespace(model=SimpleNamespace(
            parameters=lambda: [SimpleNamespace(device=SimpleNamespace(type="cpu"))],
            eval=lambda: None,
            vad_opts=SimpleNamespace(max_single_segment_time=options.get(
                "max_single_segment_time", 60000,
            )),
        ))

    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=factory))
    model = paraformer.load_paraformer_model(tmp_path / "asr", tmp_path / "vad", cpu_threads=4)
    assert model.cpu_threads == 4
    assert model.vad_device == "cpu"
    options = [call for call in calls if isinstance(call, dict)]
    assert len(options) == 2
    for option in options:
        assert option["device"] == "cpu"
        assert option["ngpu"] == 0
        assert option["ncpu"] == 4
        assert option["disable_update"] is True
        assert option["check_latest"] is False
        assert all(option[name] is None for name in ("punc_model", "spk_model", "vad_model", "lm_model"))
    assert paraformer.os.environ["CUDA_VISIBLE_DEVICES"] == ""
    assert paraformer.os.environ["HF_HUB_OFFLINE"] == "1"
    assert options[1]["max_single_segment_time"] == 30000


def test_default_cpu_threads_are_sixteen_and_cuda_remains_hidden():
    env = paraformer.cpu_environment()
    assert paraformer.DEFAULT_CPU_THREADS == 16
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert env["OMP_WAIT_POLICY"] == "PASSIVE"
    assert all(env[key] == "16" for key in (
        "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    ))


def test_gpu_environment_keeps_external_cuda_visibility_and_offline_flags(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    env = paraformer.runtime_environment("gpu", 16)
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert {**paraformer.os.environ, **env}["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    with pytest.raises(ValueError, match="device"):
        paraformer.runtime_environment("auto")


@pytest.mark.parametrize("cuda_available,parameter_device,error", [
    (False, "cpu", "requires available CUDA"),
    (True, "cpu", "must stay on cuda"),
    (True, "cuda", None),
])
def test_gpu_loader_has_no_silent_cpu_fallback(tmp_path, monkeypatch, cuda_available,
                                             parameter_device, error):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(paraformer, "_validate_component", lambda path, kind: Path(path))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None,
    ))
    calls = []

    def factory(**options):
        calls.append(options)
        actual_device = parameter_device if len(calls) == 1 else "cpu"
        return SimpleNamespace(model=SimpleNamespace(
            parameters=lambda: [SimpleNamespace(device=SimpleNamespace(type=actual_device))],
            eval=lambda: None,
            vad_opts=SimpleNamespace(max_single_segment_time=30000),
        ))

    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=factory))
    if error:
        with pytest.raises(RuntimeError, match=error):
            paraformer.load_paraformer_model(tmp_path, tmp_path, device="gpu")
    else:
        model = paraformer.load_paraformer_model(tmp_path, tmp_path, device="gpu")
        assert model.device == "gpu"
        assert model.vad_device == "cpu"
        assert len(calls) == 2
        assert calls[0]["device"] == "cuda:0" and calls[0]["ngpu"] == 1
        assert calls[1]["device"] == "cpu" and calls[1]["ngpu"] == 0
        assert calls[1]["max_single_segment_time"] == 30000


@pytest.mark.parametrize("vad_device,actual_vad_device,error", [
    ("cpu", "cuda", "must stay on cpu"),
    ("gpu", "cpu", "must stay on cuda"),
    ("gpu", "cuda", None),
])
def test_vad_placement_is_verified_independently_of_gpu_asr(
    tmp_path, monkeypatch, vad_device, actual_vad_device, error,
):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(paraformer, "_validate_component", lambda path, kind: Path(path))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True),
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None,
    ))
    calls = []

    def factory(**options):
        calls.append(options)
        placement = "cuda" if len(calls) == 1 else actual_vad_device
        return SimpleNamespace(model=SimpleNamespace(
            parameters=lambda: [SimpleNamespace(device=SimpleNamespace(type=placement))],
            eval=lambda: None, vad_opts=SimpleNamespace(max_single_segment_time=30000),
        ))

    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=factory))
    if error:
        with pytest.raises(RuntimeError, match=error):
            paraformer.load_paraformer_model(
                tmp_path, tmp_path, device="gpu", vad_device=vad_device,
            )
    else:
        model = paraformer.load_paraformer_model(
            tmp_path, tmp_path, device="gpu", vad_device=vad_device,
        )
        assert model.vad_device == "gpu"
        assert calls[1]["device"] == "cuda:0" and calls[1]["ngpu"] == 1


@pytest.mark.parametrize("device,vad_device", [("cpu", "gpu"), ("gpu", "auto")])
def test_invalid_vad_device_is_rejected_before_loading(tmp_path, device, vad_device):
    with pytest.raises(ValueError, match="VAD"):
        paraformer.load_paraformer_model(
            tmp_path, tmp_path, device=device, vad_device=vad_device,
        )


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_worker_launch_selects_matching_runtime_and_hides_cuda_only_for_cpu(tmp_path, monkeypatch, device):
    import paraformer_runtime

    monkeypatch.setattr(transcriber, "ROOT", tmp_path)
    monkeypatch.setattr(transcriber, "paraformer_runtime_is_ready", lambda value: value == device)
    monkeypatch.setattr(transcriber, "paraformer_runtime_python", lambda value: tmp_path / value / "python.exe")
    monkeypatch.setattr(paraformer_runtime, "require_cuda", lambda: None)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=io.StringIO(), stdin=io.StringIO(), poll=lambda: 0)

    monkeypatch.setattr(transcriber.subprocess, "Popen", spawn)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = transcriber.ParaformerWorker(device=device)
    worker.reader.join(timeout=2)
    assert calls[0][0][0] == str(tmp_path / device / "python.exe")
    assert calls[0][0][-2:] == ["--device", device]
    assert ("CUDA_VISIBLE_DEVICES" in calls[0][1]["env"]) is (device == "cpu")
    worker.close()


def test_switching_paraformer_device_rebuilds_worker_and_closes_old_process(tmp_path, monkeypatch):
    import asr_models

    devices = iter(["cpu", "cpu", "gpu"])
    launched, closed = [], []

    class Worker:
        def __init__(self, device):
            self.device = device
            launched.append(device)

        def transcribe(self, *args):
            return [], 1000

        def close(self):
            closed.append(self.device)

    monkeypatch.setattr(transcriber, "ParaformerWorker", Worker)
    monkeypatch.setattr(transcriber, "QwenWorker", Worker)
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_: None)
    monkeypatch.setattr(transcriber, "selected_paraformer_device", lambda: next(devices))
    monkeypatch.setattr(transcriber, "component_paths", lambda _: {})
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    transcriber.unload_model()
    try:
        with transcriber.asr_session():
            for _ in range(3):
                transcriber.transcribe_audio("video.mp4", model_id="paraformer-large-cpu")
            assert launched == ["cpu", "gpu"]
            assert closed == ["cpu"]
        assert closed == ["cpu", "gpu"]
    finally:
        transcriber.unload_model()


def test_loader_rejects_missing_local_files_before_importing_model(tmp_path):
    with pytest.raises(ValueError, match="incomplete"):
        paraformer.load_paraformer_model(tmp_path, tmp_path)


def write_audio(path, seconds=6):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(np.zeros(seconds * 16000, dtype="<i2").tobytes())


def test_interval_order_is_stable_and_never_changes_original_intervals():
    intervals = [(0, 1000), (2000, 2300), (3000, 4000), (5000, 5500)]
    original = intervals.copy()
    assert paraformer.ordered_interval_indices(intervals) == [1, 3, 0, 2]
    assert paraformer.ordered_interval_indices(intervals, "time") == [0, 1, 2, 3]
    assert intervals == original
    assert paraformer.ordered_interval_indices([]) == []
    with pytest.raises(ValueError, match="batch_order"):
        paraformer.ordered_interval_indices(intervals, "invalid")


@pytest.mark.parametrize("empty_shortest", [False, True])
@pytest.mark.parametrize("batch_order", ["length", "time"])
def test_sorted_batches_restore_original_word_times_and_ids(
    tmp_path, monkeypatch, empty_shortest, batch_order,
):
    intervals = [(0, 1200), (2000, 2200), (3000, 3800), (5000, 6200), (7000, 7600)]

    def decode(path, destination, progress):
        write_audio(destination, 8)
        return 8000

    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(paraformer, "detect_speech", lambda *_args, **_kwargs: intervals)
    calls = []

    def generate(**kwargs):
        durations = [round(len(audio) / 16) for audio in kwargs["input"]]
        calls.append(durations)
        return [
            {"text": "", "timestamp": []} if empty_shortest and duration == 200 else
            {"text": str(duration), "timestamp": [[20, 100]]}
            for duration in durations
        ]

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16)
    audit = {"raw_output_path": str(tmp_path / "raw.jsonl")}
    result, duration = paraformer.transcribe_paraformer(
        model, "video.mp4", batch_size=2, batch_order=batch_order,
        temporary_directory=tmp_path / "temp", audit=audit,
    )
    expected_order = [1, 4, 2, 0, 3] if batch_order == "length" else [0, 1, 2, 3, 4]
    assert calls == [
        [intervals[i][1] - intervals[i][0] for i in expected_order[start:start + 2]]
        for start in range(0, 5, 2)
    ]
    expected_intervals = [span for span in intervals
                          if not (empty_shortest and span[1] - span[0] == 200)]
    assert duration == 8000
    assert [segment.id for segment in result] == list(range(1, len(result) + 1))
    assert [segment.start_ms for segment in result] == [
        start + 20 for start, _ in expected_intervals
    ]
    assert [segment.words[0]["start"] for segment in result] == [
        (start + 20) / 1000 for start, _ in expected_intervals
    ]
    assert audit["speech_intervals_ms"] == intervals
    assert audit["input_order"] == expected_order
    assert audit["batch_sizes"] == [2, 2, 1]
    assert [index for batch in audit["batches"] for index in batch["original_indices"]] == (
        expected_order
    )
    assert audit["estimated_padded_seconds"] == pytest.approx(
        4.8 if batch_order == "length" else 5.4,
    )
    raw = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    assert [item["interval_index"] for item in raw] == expected_order
    assert [item["span_ms"] for item in raw] == [list(intervals[i]) for i in expected_order]


def test_sorted_batch_restores_multiple_subtitles_from_each_interval(tmp_path, monkeypatch):
    def decode(path, destination, progress):
        write_audio(destination, 9)
        return 9000

    intervals = [(0, 2500), (4000, 4500), (6000, 8500)]
    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(paraformer, "detect_speech", lambda *_args, **_kwargs: intervals)

    def generate(**kwargs):
        return [
            {"text": "a b", "timestamp": [[20, 100], [1500, 1600]]}
            if len(audio) > 16000 else {"text": "c", "timestamp": [[20, 100]]}
            for audio in kwargs["input"]
        ]

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16)
    result, _ = paraformer.transcribe_paraformer(
        model, "video.mp4", batch_size=2, temporary_directory=tmp_path,
    )
    assert [segment.id for segment in result] == [1, 2, 3, 4, 5]
    assert [segment.start_ms for segment in result] == [20, 1500, 4020, 6020, 7500]


def test_no_speech_does_not_call_asr_after_length_sorting(tmp_path, monkeypatch):
    def decode(path, destination, progress):
        write_audio(destination)
        return 6000

    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(paraformer, "detect_speech", lambda *_args, **_kwargs: [])
    calls = []
    model = paraformer.ParaformerModels(
        SimpleNamespace(generate=lambda **kwargs: calls.append(kwargs)), object(), 16,
    )
    audit = {}
    result, duration = paraformer.transcribe_paraformer(
        model, "video.mp4", temporary_directory=tmp_path, audit=audit,
    )
    assert result == [] and duration == 6000
    assert calls == []
    assert audit["input_order"] == []
    assert audit["estimated_padded_seconds"] == 0


def test_pcm_normalization_preserves_all_int16_values(tmp_path, monkeypatch):
    samples = np.arange(-32768, 32768, dtype=np.int16)
    expected = samples.astype(np.float32) / 32768

    def decode(_path, destination, _progress):
        with wave.open(str(destination), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", ""))
            output.writeframes(samples.tobytes())
        return 4096

    def generate(**kwargs):
        actual = kwargs["input"][0]
        assert actual.dtype == np.float32
        np.testing.assert_array_equal(actual, expected)
        return [{"text": "a", "timestamp": [[20, 100]]}]

    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(paraformer, "detect_speech", lambda *_a, **_k: [(0, 4096)])
    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 16)
    result, duration = paraformer.transcribe_paraformer(model, "source", temporary_directory=tmp_path)
    assert result[0].text == "a" and duration == 4096


def test_long_audio_is_batched_by_four_real_utterances_and_preserves_silence(tmp_path, monkeypatch):
    def decode(path, destination, progress):
        write_audio(destination, 6)
        return 6000

    monkeypatch.setattr(paraformer, "stream_decode_audio_to_wav", decode)
    vad_calls = []

    def detect(*args, **kwargs):
        vad_calls.append(kwargs)
        return [(i * 1000, i * 1000 + 800) for i in range(5)]

    monkeypatch.setattr(paraformer, "detect_speech", detect)
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return [{"text": "你 好", "timestamp": [[100, 200], [200, 400]]}
                for _ in kwargs["input"]]

    model = paraformer.ParaformerModels(SimpleNamespace(generate=generate), object(), 4)
    audit = {"raw_output_path": str(tmp_path / "raw.jsonl")}
    result, duration = paraformer.transcribe_paraformer(
        model, "video.mp4", batch_size=4, temporary_directory=tmp_path / "temp", audit=audit,
    )
    assert duration == 6000
    assert vad_calls == [{"batch_size": 4}]
    assert audit["vad_batch_size"] == audit["batch_size"] == 4
    assert audit["vad_device"] == "cpu"
    assert [len(call["input"]) for call in calls] == [4, 1]
    assert all(call["batch_size"] == 4 for call in calls)
    assert all(call["pred_timestamp"] is True for call in calls)
    assert all("batch_size_s" not in call for call in calls)
    assert [segment.start_ms for segment in result] == [100, 1100, 2100, 3100, 4100]
    assert audit["batch_sizes"] == [4, 1]
    assert audit["batches"][0]["output_count"] == 4
    assert set(audit["stage_seconds"]) == {"audio", "vad", "asr", "validation"}
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 5


@pytest.mark.parametrize("batch_size", [1, 4, 16])
def test_vad_uses_one_offline_file_call_and_complete_intervals(tmp_path, batch_size):
    path = tmp_path / "timeline.wav"
    write_audio(path, 2)
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return [{"value": [[100, 900], [1100, 1900]]}]

    assert paraformer.detect_speech(
        SimpleNamespace(generate=generate), path, batch_size=batch_size,
    ) == [
        (100, 900), (1100, 1900),
    ]
    assert len(calls) == 1
    assert calls[0]["input"] == str(path)
    assert calls[0]["is_final"] is True
    assert calls[0]["is_streaming_input"] is False
    assert calls[0]["chunk_size"] == 60000
    assert calls[0]["batch_size"] == batch_size
    assert calls[0]["max_end_silence_time"] == 800
    assert calls[0]["dynamic_silence"] is False
    assert "cache" not in calls[0]
    assert "max_single_segment_time" not in calls[0]


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5])
def test_vad_rejects_invalid_batch_size_before_reading_audio(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        paraformer.detect_speech(object(), tmp_path / "missing.wav", batch_size=batch_size)


@pytest.mark.parametrize("value", [[[100, -1]], [[-1, 900]], [[True, 900]],
                                 [[100.0, 900]], [[100, 900, 1000]]])
def test_offline_vad_rejects_partial_or_invalid_intervals(tmp_path, value):
    path = tmp_path / "timeline.wav"
    write_audio(path, 2)
    vad = SimpleNamespace(generate=lambda **kwargs: [{"value": value}])
    with pytest.raises(ValueError):
        paraformer.detect_speech(vad, path)


@pytest.mark.parametrize("intervals", [
    [(0, 1000), (900, 1500)], [(0, 9000)], [(-1, 100)], [(0, 0)],
])
def test_vad_interval_validation_rejects_invalid_timeline(intervals):
    with pytest.raises(ValueError):
        paraformer.validate_intervals(intervals, 2000)


@pytest.mark.parametrize("batch_size", [None, 8, 16])
def test_worker_protocol_preserves_automatic_or_explicit_batch_and_subtitle_contract(tmp_path, batch_size):
    worker = transcriber.ParaformerWorker.__new__(transcriber.ParaformerWorker)
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({
        "event": "result", "duration_ms": 2000, "segments": [{
            "id": 1, "start_ms": 100, "end_ms": 1800, "text": "你好", "words": [
                {"word": "你", "start": .1, "end": .4, "probability": None},
            ],
        }],
    }))
    result, duration = worker.transcribe(
        "video.mp4", {"recognizer": tmp_path / "asr", "vad": tmp_path / "vad"}, None,
        batch_size=batch_size,
    )
    request = json.loads(worker.process.stdin.getvalue())
    if batch_size is None:
        assert "batch_size" not in request
    else:
        assert request["batch_size"] == batch_size
    assert "cpu_threads" not in request
    assert request["vad"] == str(tmp_path / "vad")
    assert not Path(request["temporary_directory"]).exists()
    assert result[0] == TranscriptSegment(
        1, 100, 1800, "你好", [{"word": "你", "start": .1, "end": .4, "probability": None}],
    )
    assert duration == 2000
