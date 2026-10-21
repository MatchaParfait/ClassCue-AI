import sys
from contextlib import nullcontext
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from core import transcript_to_vtt
import qwen_transcriber


def word(text, start, end):
    return SimpleNamespace(text=text, start_time=start, end_time=end)


def test_alignment_preserves_punctuation_ids_and_absolute_word_timestamps():
    items = [word("单", .1, .3), word("纯", .3, .5), word("形", .5, .7),
             word("法", .7, .9), word("KKT", 1.5, 1.9), word("条件", 1.9, 2.5)]
    segments = qwen_transcriber.aligned_segments("单纯形法。KKT 条件！", items, 25, 3, first_id=8)
    assert [segment.id for segment in segments] == [8, 9]
    assert [segment.text for segment in segments] == ["单纯形法。", "KKT 条件！"]
    assert (segments[0].start_ms, segments[0].end_ms) == (25100, 25900)
    assert segments[1].words[0]["start"] == 26.5
    assert segments[1].words[0]["probability"] is None
    assert "00:00:25.100 --> 00:00:25.900" in transcript_to_vtt(segments)


def test_alignment_splits_at_long_silence_without_joining_unrelated_speech():
    segments = qwen_transcriber.aligned_segments("你好再见", [
        word("你好", 0, 1), word("再见", 4, 5),
    ], 0, 6)
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [(0, 1000), (4000, 5000)]
    assert "".join(segment.text for segment in segments) == "你好再见"


def test_alignment_matches_words_with_internal_punctuation():
    segments = qwen_transcriber.aligned_segments(
        "1,234 和 x+y。", [word("1234", 0, .5), word("和", .5, .8), word("xy", .8, 1.5)], 0, 2
    )
    assert segments[0].text == "1,234 和 x+y。"


@pytest.mark.parametrize("items", [
    [], [word("例", float("nan"), 1)], [word("例", 2, 1)],
    [word("例", 0, 8)], [word("错误", 0, 1)],
])
def test_invalid_or_missing_alignment_fails_instead_of_fabricating_timestamps(items):
    with pytest.raises(ValueError):
        qwen_transcriber.aligned_segments("例题", items, 0, 3)


def test_silent_audio_has_no_cues():
    assert qwen_transcriber.aligned_segments("", [], 0, 2) == []


def test_zero_duration_aligned_words_produce_valid_vtt_cue():
    segment = qwen_transcriber.aligned_segments("好", [word("好", 0, 0)], 0, 1)[0]
    assert segment.end_ms > segment.start_ms


def test_qwen_uses_official_long_audio_pipeline_and_absolute_timestamps(monkeypatch):
    audio = np.zeros(26 * 16000, dtype=np.float32)
    audio[8000] = 0.1
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _path: audio)
    requests = []

    class Model:
        def transcribe(self, **kwargs):
            requests.append(kwargs)
            return [SimpleNamespace(text="考点。", time_stamps=SimpleNamespace(
                items=[word("考", 25.1, 25.3), word("点", 25.3, 25.8)],
            ))]

    segments, duration = qwen_transcriber.transcribe_qwen(Model(), "video.mp4")
    assert duration == 26000
    assert [segment.id for segment in segments] == [1]
    assert segments[0].start_ms == 25100
    assert all(request["return_time_stamps"] is True for request in requests)
    assert all(request["language"] == "Chinese" for request in requests)
    assert requests[0]["audio"][1] is not None


def test_qwen_model_loading_is_local_only_and_8gb_gpu_uses_cpu_aligner(monkeypatch):
    calls = []
    torch = SimpleNamespace(
        float16="float16", float32="float32",
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None,
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_properties=lambda _index: SimpleNamespace(total_memory=8 * 1024**3),
        ),
    )
    factory = SimpleNamespace(from_pretrained=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(Qwen3ASRModel=factory))
    monkeypatch.setattr(qwen_transcriber, "_checkpoint_reader", nullcontext)
    qwen_transcriber.load_qwen_model("recognizer", "aligner")
    _args, kwargs = calls[0]
    assert kwargs["local_files_only"] is True
    assert kwargs["max_inference_batch_size"] == 1
    assert kwargs["device_map"] == "cuda:0"
    assert kwargs["forced_aligner_kwargs"]["device_map"] == "cpu"
    assert kwargs["forced_aligner_kwargs"]["local_files_only"] is True


def test_qwen_model_loading_allows_two_item_batches_on_larger_gpu(monkeypatch):
    calls = []
    torch = SimpleNamespace(
        float16="float16", float32="float32",
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None,
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_properties=lambda _index: SimpleNamespace(total_memory=16 * 1024**3),
        ),
    )
    factory = SimpleNamespace(from_pretrained=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(Qwen3ASRModel=factory))
    monkeypatch.setattr(qwen_transcriber, "_checkpoint_reader", nullcontext)
    qwen_transcriber.load_qwen_model("recognizer", "aligner")
    _args, kwargs = calls[0]
    assert kwargs["max_inference_batch_size"] == 2
    assert kwargs["forced_aligner_kwargs"]["device_map"] == "cuda:0"


def test_staged_asr_loader_does_not_load_aligner(monkeypatch):
    calls = []
    torch = SimpleNamespace(
        float16="float16", float32="float32",
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None,
        cuda=SimpleNamespace(is_available=lambda: True,
                             get_device_properties=lambda _: SimpleNamespace(total_memory=8 * 1024**3)),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(
        Qwen3ASRModel=SimpleNamespace(from_pretrained=lambda *a, **kw: calls.append(kw)),
    ))
    monkeypatch.setattr(qwen_transcriber, "_checkpoint_reader", nullcontext)
    qwen_transcriber.load_qwen_model("asr", "align", load_aligner=False)
    assert calls[0]["forced_aligner"] is None
    assert calls[0]["dtype"] == "float16"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only checkpoint reader")
def test_windows_checkpoint_reader_uses_pread_and_restores_on_failure(monkeypatch):
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return "checkpoint"

    modeling_utils = SimpleNamespace(safe_open=original)
    monkeypatch.setitem(sys.modules, "safetensors", SimpleNamespace(safe_open=original))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(modeling_utils=modeling_utils))
    with pytest.raises(RuntimeError):
        with qwen_transcriber._checkpoint_reader():
            assert modeling_utils.safe_open("model.safetensors", framework="pt") == "checkpoint"
            raise RuntimeError("load failed")
    assert calls == [(("model.safetensors",), {"backend": "pread", "framework": "pt"})]
    assert modeling_utils.safe_open is original


def test_pyav_decoder_preserves_silence_and_original_duration(tmp_path):
    path = tmp_path / "sample.wav"
    samples = np.concatenate([np.zeros(16000, dtype=np.int16), np.full(16000, 1000, dtype=np.int16)])
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    decoded = qwen_transcriber.decode_audio(path)
    assert len(decoded) == 32000
    assert decoded.dtype == np.float32
    assert np.max(np.abs(decoded[:15000])) == 0
    assert np.max(np.abs(decoded[17000:])) > 0


def test_decoder_keeps_delayed_audio_on_original_video_timeline(monkeypatch):
    from fractions import Fraction
    import av

    frame = SimpleNamespace(
        pts=16000, time_base=Fraction(1, 16000),
        to_ndarray=lambda: np.ones((1, 16000), dtype=np.float32),
    )

    class Container:
        streams = SimpleNamespace(audio=[object()])
        start_time = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def decode(self, **_kwargs):
            yield frame

    resampler = SimpleNamespace(resample=lambda item: [item] if item is not None else [])
    monkeypatch.setattr(av, "open", lambda *_args: Container())
    monkeypatch.setattr(av, "AudioResampler", lambda **_kwargs: resampler)
    samples = qwen_transcriber.decode_audio("delayed.mp4")
    assert len(samples) == 32000
    assert np.max(np.abs(samples[:16000])) == 0
    assert np.min(samples[16000:]) == 1


def test_stream_decoder_writes_incrementally_and_preserves_pts_gap(monkeypatch, tmp_path):
    from fractions import Fraction
    import av

    frame = SimpleNamespace(
        pts=16000, time_base=Fraction(1, 16000),
        samples=16000, sample_rate=16000,
        to_ndarray=lambda: np.ones((1, 16000), dtype=np.float32),
    )

    class Container:
        streams = SimpleNamespace(audio=[object()])
        start_time = 0
        duration = 32000

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def demux(self, **_kwargs):
            yield SimpleNamespace(decode=lambda: [frame])

    resampler = SimpleNamespace(resample=lambda item: [item] if item is not None else [])
    monkeypatch.setattr(av, "open", lambda *_args: Container())
    monkeypatch.setattr(av, "AudioResampler", lambda **_kwargs: resampler)
    output = tmp_path / "timeline.wav"
    progress = []
    duration = qwen_transcriber.stream_decode_audio_to_wav(
        "delayed.mp4", output, lambda fraction, message: progress.append((fraction, message)),
    )
    with wave.open(str(output), "rb") as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert len(samples) == 32000
        assert np.max(np.abs(samples[:16000])) == 0
        assert np.min(samples[16000:]) > 0
    assert duration == 2000
    assert progress


def test_stream_decoder_rejects_empty_audio(monkeypatch, tmp_path):
    import av

    class Container:
        streams = SimpleNamespace(audio=[object()])
        start_time = 0
        duration = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def demux(self, **_kwargs):
            return iter(())

    monkeypatch.setattr(av, "open", lambda *_args: Container())
    monkeypatch.setattr(av, "AudioResampler", lambda **_kwargs:
                        SimpleNamespace(resample=lambda item: []))
    with pytest.raises(ValueError, match="音频轨道为空"):
        qwen_transcriber.stream_decode_audio_to_wav("empty.mp4", tmp_path / "empty.wav")


def test_decoder_preserves_one_second_audio_delay_in_real_video(tmp_path):
    from fractions import Fraction
    import av

    path = tmp_path / "delayed.mkv"
    with av.open(str(path), "w") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 64, 64, "yuv420p"
        audio = container.add_stream("pcm_s16le", rate=16000)
        audio.layout = "mono"
        for index in range(20):
            frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24")
            frame.pts = index
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        frame = av.AudioFrame.from_ndarray(
            np.full((1, 16000), 1000, dtype=np.int16), format="s16", layout="mono",
        )
        frame.sample_rate, frame.time_base, frame.pts = 16000, Fraction(1, 16000), 16000
        for packet in audio.encode(frame):
            container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)
    samples = qwen_transcriber.decode_audio(path)
    assert len(samples) == 32000
    assert np.max(np.abs(samples[:16000])) == 0
    assert np.min(samples[16000:]) > 0
