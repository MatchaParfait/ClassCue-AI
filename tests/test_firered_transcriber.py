from fractions import Fraction
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

import firered_transcriber
from firered_transcriber import TimedToken, _segments_from_tokens, _tokens


class NativeSystem:
    """Test double with the pinned official system's input/output contract."""

    def __init__(self, spans, recognize):
        self.vad = SimpleNamespace(detect=lambda _path: ({"timestamps": spans}, None))
        self.asr = SimpleNamespace(transcribe=recognize)
        self.process_calls = []

    def process(self, path, uttid):
        self.process_calls.append(path)
        with wave.open(path, "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        detection, _ = self.vad.detect(path)
        sentences, words = [], []
        for start, end in detection["timestamps"]:
            ids = [f"{uttid}_s{int(start * 1000)}_e{int(end * 1000)}"]
            outputs = self.asr.transcribe(ids, [(16000, audio[int(start * 16000):int(end * 16000)])])
            for output in outputs:
                if not output["text"] or output["text"] in ("<blank>", "<sil>"):
                    continue
                sentences.append({"text": output["text"], "start_ms": int(start * 1000),
                                  "end_ms": int(end * 1000)})
                words.extend({"text": text, "start_ms": int(s * 1000 + int(start * 1000)),
                              "end_ms": int(e * 1000 + int(start * 1000))}
                             for text, s, e in output.get("timestamp", []))
        return {"sentences": sentences, "words": words}


def patch_stream(monkeypatch, audio):
    def stream(_path, output_path, _progress=None):
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(np.clip(np.rint(audio * 32768), -32768, 32767)
                            .astype("<i2").tobytes())
        return round(len(audio) / 16000 * 1000)

    monkeypatch.setattr(firered_transcriber, "stream_decode_audio_to_wav", stream)


def test_official_timestamp_tuples_are_normalized():
    assert _tokens({"timestamp": [("hello", .2, .5), ("world", .6, 1)]}) == [
        TimedToken("hello", .2, .5), TimedToken("world", .6, 1),
    ]


def test_empty_sentencepiece_boundaries_do_not_remove_real_words():
    tokens = _tokens({"timestamp": [("", .02, .1), ("你", .1, .2), (" ", .2, .3), ("好", .3, .5)]})
    assert tokens == [TimedToken("你", .1, .2), TimedToken("好", .3, .5)]
    segments = _segments_from_tokens(tokens, "你好", duration_seconds=1)
    assert segments[0].text == "你好"
    assert (segments[0].start_ms, segments[0].end_ms) == (100, 500)


def test_unknown_model_tokens_and_punctuation_preserve_text_and_real_timing():
    # Both real failed batches contained this token sequence. Unknown markers
    # must neither abort a whole course nor silently discard the utterance.
    tokens = _tokens({"timestamp": [
        ("", .02, .1), ("<unk>", .1, .2), ("s", .2, .3),
        ("p", .3, .4), ("k", .4, .5), ("<unk>", .5, .6),
    ]})
    result = _segments_from_tokens(tokens, "<unk>spk<unk>", duration_seconds=1)
    assert result[0].text == "<unk>spk<unk>"
    assert (result[0].start_ms, result[0].end_ms) == (100, 600)
    with pytest.raises(ValueError, match="不一致|未覆盖"):
        _segments_from_tokens(tokens, "<unk>spoken<unk>", duration_seconds=1)


@pytest.mark.parametrize("timestamp", [
    [{"start": 0, "end": 1}], [(None, 0, 1)], [("", None, 1)], [("", float("nan"), 1)],
])
def test_malformed_entries_are_not_mistaken_for_empty_boundary_tokens(timestamp):
    with pytest.raises(ValueError):
        _tokens({"timestamp": timestamp})


def test_empty_boundary_normalization_survives_native_merge_streaming_and_resume(monkeypatch, tmp_path):
    from firered_resilience import FireRedCheckpoints
    patch_stream(monkeypatch, np.zeros(3 * 16000, dtype=np.float32))
    checkpoints = FireRedCheckpoints(tmp_path, "boundary-test", 3 * 16000)
    spans = [(1, 2), (2, 3)]
    checkpoints.save_vad({"timestamps": spans})
    # Also cover old/raw cached outputs; fresh and cached tokens must match.
    cached = checkpoints.chunk(0, spans[0])
    checkpoints.save(cached, {"uttid": cached["uttid"], "text": "hello",
                             "timestamp": [("", .02, .1), ("hello", .1, .9)]})
    calls = []
    def recognize(ids, _waves):
        calls.extend(ids)
        return [{"uttid": ids[0], "text": "hello", "timestamp": [("", .02, .1), ("hello", .1, .9)]}]
    system = NativeSystem(spans, recognize)
    for _ in range(2):
        streamed = []
        result, duration = firered_transcriber.transcribe_firered(
            system, "lesson.mp4", checkpoints=checkpoints,
            on_transcript=lambda added, watermark: streamed.extend(added),
        )
        assert duration == 3000
        assert streamed == result
        assert [item.start_ms for item in result] == [1100, 2100]
        assert [item.text for item in result] == ["hello", "hello"]
    assert calls == ["video_s2000_e3000"]
    assert checkpoints.counts() == (2, 2)


def test_segments_preserve_english_spaces_and_punctuation_without_duplication():
    segments = _segments_from_tokens(
        [TimedToken("hello", 0, 1), TimedToken("world", 20, 21)],
        "hello world.", first_id=3, duration_seconds=22,
    )
    assert [segment.id for segment in segments] == [3, 4]
    assert [segment.text for segment in segments] == ["hello", "world."]
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [
        (0, 1000), (20000, 21000),
    ]
    single = _segments_from_tokens(
        [TimedToken("hello", 0, 1), TimedToken("world", 1, 2)],
        "hello world", duration_seconds=3,
    )
    assert single[0].text == "hello world"


@pytest.mark.parametrize("tokens", [
    [], [TimedToken("hello", float("nan"), 1)], [TimedToken("hello", 2, 1)],
    [TimedToken("hello", 0, float("inf"))], [TimedToken("hello", 0, 99)],
    [TimedToken("wrong", 0, 1)],
])
def test_invalid_or_missing_alignment_is_rejected(tokens):
    with pytest.raises(ValueError):
        _segments_from_tokens(tokens, "hello", duration_seconds=3)


def test_silent_output_has_no_cues():
    assert _segments_from_tokens([], "", duration_seconds=3) == []


@pytest.mark.parametrize("gpu", [True, False])
@pytest.mark.parametrize("explicit_cpu", [True, False])
def test_model_loader_uses_official_aed_api_and_requests_timestamps(monkeypatch, gpu, explicit_cpu):
    import qwen_memory

    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", lambda **_: None)
    requests = []

    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    model = SimpleNamespace(
        model=SimpleNamespace(ctc=SimpleNamespace(register_forward_hook=lambda _hook: None)),
        _get_and_fix_timestamp=lambda *_args: [],
    )
    system = SimpleNamespace(asr=model)
    def available():
        if explicit_cpu:
            pytest.fail("Explicit CPU loading must not probe CUDA")
        return gpu

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=available),
        load=lambda *_args, **_kwargs: None,
    ))
    monkeypatch.setitem(sys.modules, "fireredasr2s.fireredasr2", SimpleNamespace(
        FireRedAsr2Config=Config,
        asr=SimpleNamespace(load_fireredasr_aed_model=lambda *_args: None),
    ))
    monkeypatch.setitem(sys.modules, "fireredasr2s.fireredvad", SimpleNamespace(
        FireRedVadConfig=Config,
    ))
    monkeypatch.setitem(sys.modules, "fireredasr2s.fireredasr2system", SimpleNamespace(
        FireRedAsr2System=lambda config: requests.append(config) or system,
        FireRedAsr2SystemConfig=Config,
    ))
    if not gpu and not explicit_cpu:
        with pytest.raises(RuntimeError, match="CPU fallback"):
            firered_transcriber.load_firered_model("local-recognizer", "local-vad/VAD")
        assert requests == []
        return
    options = {"use_gpu": False, "batch_size": 3} if explicit_cpu else {"batch_size": 6}
    assert firered_transcriber.load_firered_model(
        "local-recognizer", "local-vad/VAD", **options,
    ) is system
    expected_gpu = gpu and not explicit_cpu
    pipeline = requests[0]
    assert pipeline.asr_type == "aed"
    assert pipeline.asr_model_dir == "local-recognizer"
    assert pipeline.vad_model_dir == "local-vad/VAD"
    assert pipeline.enable_vad is True
    assert pipeline.enable_lid is pipeline.enable_punc is False
    assert pipeline.asr_batch_size == (3 if explicit_cpu else 6)
    assert pipeline.vad_config.use_gpu is expected_gpu
    config = pipeline.asr_config
    assert config.return_timestamp is True
    assert config.use_gpu is expected_gpu
    assert config.use_half is expected_gpu
    if not gpu and not explicit_cpu:
        with pytest.raises(RuntimeError, match="GPU"):
            firered_transcriber.load_firered_model("recognizer", "vad", use_gpu=True)


def test_model_loader_rejects_invalid_batch_size():
    with pytest.raises(ValueError, match="batch size"):
        firered_transcriber.load_firered_model("recognizer", "vad", batch_size=0)


def test_cpu_processing_rechecks_memory_before_batches(monkeypatch):
    import qwen_memory

    calls = []
    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", lambda **kw: calls.append(kw))
    for use_gpu in (True, False):
        system = SimpleNamespace(asr=SimpleNamespace(config=SimpleNamespace(use_gpu=use_gpu)))
        firered_transcriber._check_cpu_headroom(system)
    assert calls == [{"physical_mib": 1024, "commit_mib": 2048}]


def test_checkpoint_mapping_preserves_official_loader_and_restores_after_failure(tmp_path):
    import zipfile

    checkpoint = tmp_path / "model.pth.tar"
    with zipfile.ZipFile(checkpoint, "w") as output:
        output.writestr("data.pkl", "fixture")
    calls = []
    original = lambda *args, **kwargs: calls.append((args, kwargs))
    torch = SimpleNamespace(load=original)
    with pytest.raises(RuntimeError):
        with firered_transcriber._memory_mapped_weights(torch):
            torch.load(checkpoint, weights_only=False)
            raise RuntimeError("load failure")
    assert calls[0][1] == {"weights_only": False, "mmap": True}
    assert torch.load is original


def test_empty_aed_loader_assigns_official_checkpoint_and_restores_hooks():
    calls = []
    register = lambda *_args: calls.append("register")
    module = SimpleNamespace(register_parameter=register)
    tensor = SimpleNamespace(requires_grad=False, to=lambda device: calls.append(device) or "empty")
    model = SimpleNamespace(
        load_state_dict=lambda state, **kwargs: calls.append((state, kwargs)),
        decoder=SimpleNamespace(tgt_word_emb=SimpleNamespace(weight=object()),
                                tgt_word_prj=SimpleNamespace(weight=object())),
    )

    def construct(args):
        assert args == "official-args"
        module.register_parameter(model, "weight", tensor)
        return model

    original = lambda *_args: None
    asr = SimpleNamespace(
        load_fireredasr_aed_model=original,
        FireRedAsrAed=SimpleNamespace(from_args=construct),
    )
    torch = SimpleNamespace(
        load=lambda *args, **kwargs: {"args": "official-args", "model_state_dict": "weights"},
        nn=SimpleNamespace(Module=module, Parameter=lambda value, **kwargs: value),
    )
    with firered_transcriber._aed_checkpoint_loader(torch, asr):
        assert asr.load_fireredasr_aed_model("checkpoint") is model
        assert module.register_parameter is register
    assert asr.load_fireredasr_aed_model is original
    assert calls == ["meta", "register", ("weights", {"strict": True, "assign": True})]
    assert model.decoder.tgt_word_prj.weight is model.decoder.tgt_word_emb.weight


def test_alignment_logits_are_float32_and_average_timestamp_fallback_is_rejected():
    hooks = []
    expected = [("课程", 0.1, 0.9)]
    model = SimpleNamespace(
        model=SimpleNamespace(ctc=SimpleNamespace(register_forward_hook=hooks.append)),
        _get_and_fix_timestamp=lambda *_args: expected,
    )
    firered_transcriber._enable_strict_timestamps(model)
    fp32 = object()
    assert hooks[0](None, None, SimpleNamespace(float=lambda: fp32)) is fp32
    with pytest.raises(ValueError, match="平均分配"):
        model._get_and_fix_timestamp({"timestamp": None}, [1], 1.0)
    assert model._get_and_fix_timestamp({"timestamp": ([0.1], [0.9])}, [1], 1.0) == expected
    assert model._get_and_fix_timestamp({"timestamp": None}, [], 1.0) == []

def test_decoder_preserves_pts_gaps_before_model_inference(monkeypatch):
    import av

    class Frame:
        def __init__(self, pts):
            self.pts, self.time_base = pts, Fraction(1, 16000)
            self.samples, self.sample_rate = 16000, 16000

        def to_ndarray(self):
            return np.full((1, 16000), .25, dtype=np.float32)

    class Container:
        streams = SimpleNamespace(audio=[object()])
        start_time = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def demux(self, **_kwargs):
            return iter([SimpleNamespace(decode=lambda: [Frame(0), Frame(160000)])])

    monkeypatch.setattr(av, "open", lambda *_args: Container())
    monkeypatch.setattr(av, "AudioResampler", lambda **_kwargs: SimpleNamespace(
        resample=lambda frame: [frame] if frame is not None else [],
    ))

    def recognize(_ids, waves):
        sample_rate, audio = waves[0]
        assert sample_rate == 16000
        assert len(audio) == 16000
        assert np.min(audio) > 0
        return [{"text": "second", "timestamp": [("second", 0, 1)]}]

    system = NativeSystem([(10, 11)], recognize)
    original_detect = system.vad.detect

    def detect(path):
        with wave.open(path, "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        assert len(audio) == 11 * 16000
        assert np.max(np.abs(audio[16000:160000])) == 0
        return original_detect(path)

    system.vad.detect = detect
    segments, duration = firered_transcriber.transcribe_firered(system, "delayed.mp4")
    assert duration == 11000
    assert (segments[0].start_ms, segments[0].end_ms) == (10000, 11000)


def test_native_vad_offsets_are_applied_once_and_progress_and_methods_are_restored(monkeypatch):
    patch_stream(monkeypatch, np.full(61 * 16000, .5, dtype=np.float32))
    requests = []

    def recognize(ids, waves):
        requests.append(ids)
        assert len(waves[0][1]) == 16000
        return [{"text": "hello world", "timestamp": [
            ("hello", .1, .4), ("world", .4, .9),
        ]}]

    system = NativeSystem([(2, 3), (60, 61)], recognize)
    originals = (system.vad.detect, system.asr.transcribe)
    progress = []
    segments, duration = firered_transcriber.transcribe_firered(
        system, "long.mp4", lambda fraction, message: progress.append((fraction, message)),
    )
    assert duration == 61000
    assert requests == [["video_s2000_e3000"], ["video_s60000_e61000"]]
    assert len(system.process_calls) == 1
    assert not firered_transcriber.Path(system.process_calls[0]).exists()
    assert (system.vad.detect, system.asr.transcribe) == originals
    assert [fraction for fraction, _ in progress] == sorted(fraction for fraction, _ in progress)
    assert any("FireRedVAD" in message for _, message in progress)
    assert any("2/2" in message for _, message in progress)
    assert progress[-1][0] == 1.0
    assert [segment.id for segment in segments] == [1, 2]
    assert [segment.start_ms for segment in segments] == [2100, 60100]
    assert segments[1].end_ms == 60900
    assert segments[1].words[0]["start"] == 60.1
    assert segments[1].words[-1]["end"] == 60.9


def test_incremental_subtitles_exactly_match_native_merge_and_arrive_before_completion(monkeypatch):
    patch_stream(monkeypatch, np.zeros(5 * 16000, dtype=np.float32))
    emitted, calls = [], []

    def recognize(ids, _waves):
        calls.append(ids)
        if len(calls) > 1:
            assert len(emitted) == len(calls) - 1
        if len(calls) == 2:
            return [{"text": "<sil>", "timestamp": []}]
        return [{"text": "hello world.", "timestamp": [
            ("hello", .1009, .4009), ("world", .4009, .9009),
        ]}]

    system = NativeSystem([(.0019, 1.0019), (2, 3), (4, 5)], recognize)
    result, _duration = firered_transcriber.transcribe_firered(
        system, "lesson.mp4",
        on_transcript=lambda added, watermark: emitted.append((added, watermark)),
    )
    assert [item for added, _ in emitted for item in added] == result
    assert [watermark for _, watermark in emitted] == [1001, 3000, 5000]
    assert emitted[1][0] == []
    assert [item.id for item in result] == [1, 2]


def test_missing_timestamps_in_worker_output_fails_instead_of_returning_1ms_cue(monkeypatch):
    patch_stream(monkeypatch, np.ones(16000, dtype=np.float32))
    model = NativeSystem([(0, 1)], lambda *_args: [{"text": "hello"}])
    original = model.asr.transcribe
    with pytest.raises(ValueError):
        firered_transcriber.transcribe_firered(model, "audio.wav")
    assert model.asr.transcribe is original
    assert not firered_transcriber.Path(model.process_calls[0]).exists()


def test_silent_model_output_does_not_create_empty_subtitles(monkeypatch):
    patch_stream(monkeypatch, np.zeros(16000, dtype=np.float32))
    model = NativeSystem([], lambda *_args: pytest.fail("Silence must not invoke AED"))
    segments, duration = firered_transcriber.transcribe_firered(model, "silence.wav")
    assert segments == []
    assert duration == 1000


def test_segment_local_invalid_timestamps_are_rejected_before_global_merge(monkeypatch):
    patch_stream(monkeypatch, np.zeros(61 * 16000))
    system = NativeSystem([(60, 61)], lambda *_args: [
        {"text": "hello", "timestamp": [("hello", 0, 2)]},
    ])
    with pytest.raises(ValueError, match="时间对齐"):
        firered_transcriber.transcribe_firered(system, "audio.wav")


@pytest.mark.parametrize("text", ["", "<blank>", "<sil>"])
def test_native_filtered_silence_outputs_do_not_create_cues(monkeypatch, text):
    patch_stream(monkeypatch, np.zeros(16000))
    system = NativeSystem([(0, 1)], lambda *_args: [{"text": text, "timestamp": []}])
    assert firered_transcriber.transcribe_firered(system, "audio.wav") == ([], 1000)


def test_temporary_audio_cleanup_retries_transient_windows_lock(monkeypatch):
    import tempfile

    attempts = []
    waits = []

    class Temporary:
        name = "temporary-audio"

        def cleanup(self):
            attempts.append(True)
            if len(attempts) < 3:
                raise PermissionError("WAV is locked")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", lambda **_kwargs: Temporary())
    monkeypatch.setattr(firered_transcriber.time, "sleep", waits.append)
    with firered_transcriber._temporary_audio_directory(None) as directory:
        assert directory == "temporary-audio"
    assert len(attempts) == 3
    assert waits == [0.2, 0.4]


@pytest.mark.parametrize("failure", [None, RuntimeError("ASR failed")])
def test_persistent_cleanup_lock_does_not_mask_result_or_asr_error(monkeypatch, caplog, failure):
    import tempfile

    attempts = []

    class Temporary:
        name = "temporary-audio"

        def cleanup(self):
            attempts.append(True)
            raise PermissionError("WAV is locked")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", lambda **_kwargs: Temporary())
    monkeypatch.setattr(firered_transcriber.time, "sleep", lambda _seconds: None)

    def run():
        with firered_transcriber._temporary_audio_directory(None):
            if failure is not None:
                raise failure
        return "ASR completed"

    if failure is None:
        assert run() == "ASR completed"
    else:
        with pytest.raises(RuntimeError) as raised:
            run()
        assert raised.value is failure
    assert len(attempts) == 4
    assert "cleanup deferred" in caplog.text


@pytest.mark.parametrize("failing_call", ["features", "decoder", "alignment"])
def test_upstream_swallowed_exception_is_rethrown_and_all_proxies_restored(monkeypatch, failing_call):
    class Oom(RuntimeError):
        pass
    error = Oom("real original failure")
    def fail(*_args, **_kwargs):
        raise error
    feature = fail if failing_call == "features" else lambda *_args: None
    decode = fail if failing_call == "decoder" else lambda *_args: None
    align = fail if failing_call == "alignment" else lambda *_args: None
    function = SimpleNamespace(forced_align=align)
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(functional=function))
    asr = SimpleNamespace(feat_extractor=feature, model=SimpleNamespace(transcribe=decode))
    system = SimpleNamespace(asr=asr)
    with firered_transcriber._native_error_capture(system) as errors:
        try:
            asr.feat_extractor()
            asr.model.transcribe()
            function.forced_align()
        except Exception:
            pass  # The pinned upstream code catches and converts these to empty results.
        assert errors[0] is error
    assert asr.feat_extractor is feature
    assert asr.model.transcribe is decode
    assert function.forced_align is align


def test_native_pipeline_resumes_committed_silence_and_speech_without_repeat(monkeypatch, tmp_path):
    from firered_resilience import FireRedCheckpoints
    from processing_control import ProcessingCancelled

    timeline = tmp_path / "timeline.wav"
    with wave.open(str(timeline), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", ""))
        output.writeframes(b"\x00\x00" * 48000)
    checkpoints = FireRedCheckpoints(tmp_path, "job", 48000)
    first_calls, second_calls = [], []
    def recognize_first(ids, _waves):
        first_calls.extend(ids)
        if len(first_calls) == 3:
            raise ProcessingCancelled("stop")
        return [{"uttid": ids[0], "text": "" if len(first_calls) == 1 else "hello",
                 "timestamp": [] if len(first_calls) == 1 else [["hello", 0, 1]]}]
    system = NativeSystem([(0, 1), (1, 2), (2, 3)], recognize_first)
    with pytest.raises(ProcessingCancelled):
        firered_transcriber.transcribe_firered(
            system, timeline, timeline_path=timeline, checkpoints=checkpoints,
        )
    assert checkpoints.counts() == (2, 3)
    assert timeline.exists()
    def recognize_second(ids, _waves):
        second_calls.extend(ids)
        return [{"uttid": ids[0], "text": "last", "timestamp": [["last", 0, 1]]}]
    system = NativeSystem([], recognize_second)
    system.vad.detect = lambda *_args: pytest.fail("Must reuse native VAD")
    metrics = {}
    segments, duration = firered_transcriber.transcribe_firered(
        system, timeline, timeline_path=timeline, checkpoints=checkpoints, metrics=metrics,
    )
    assert second_calls == ["video_s2000_e3000"]
    assert duration == 3000
    assert [segment.text for segment in segments] == ["hello", "last"]
    assert [segment.start_ms for segment in segments] == [1000, 2000]
    assert metrics["cached_segments"] == 2
    assert checkpoints.result() == (segments, duration)


def test_invalid_batch_does_not_commit_any_partial_result(tmp_path):
    from firered_resilience import FireRedCheckpoints
    checkpoints = FireRedCheckpoints(tmp_path, "job", 16000)
    checkpoints.save_vad({"timestamps": [[0, 1]]})
    timeline = tmp_path / "timeline.wav"
    with wave.open(str(timeline), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", ""))
        output.writeframes(b"\x00\x00" * 16000)
    system = NativeSystem([(0, 1)], lambda ids, _waves: [
        {"uttid": ids[0], "text": "hello", "timestamp": []},
    ])
    with pytest.raises(ValueError):
        firered_transcriber.transcribe_firered(
            system, timeline, timeline_path=timeline, checkpoints=checkpoints,
        )
    assert checkpoints.counts() == (0, 1)


def test_alignment_oom_is_not_hidden_by_later_missing_timestamp_error(monkeypatch):
    class Oom(RuntimeError):
        pass
    error = Oom("alignment CUDA allocation failure")
    def fail(*_args, **_kwargs):
        raise error
    function = SimpleNamespace(forced_align=fail)
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(functional=function))
    patch_stream(monkeypatch, np.zeros(16000))
    def recognize(*_args):
        try:
            function.forced_align()
        except Exception:
            pass
        raise ValueError("Strict timestamp validation now fails")
    system = NativeSystem([(0, 1)], recognize)
    system.asr.model = SimpleNamespace(transcribe=lambda *_args: [])
    phases = []
    with pytest.raises(Oom) as raised:
        firered_transcriber.transcribe_firered(system, "audio.wav", on_phase=phases.append)
    assert raised.value is error
    assert phases[-1] == "alignment"
    assert function.forced_align is fail
