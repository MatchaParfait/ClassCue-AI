from types import SimpleNamespace
import sys
import wave

import numpy as np
import pytest

import qwen_transcriber
from qwen_silence import resolve_silence_skipping
from scripts import benchmark_qwen_silence, qwen_chunked
from transcriber import QwenWorker


@pytest.mark.parametrize(("profile", "override", "expected"), [
    ("general", None, False),
    ("mostly-silent", None, True),
    ("general", True, True),
    ("general", False, False),
    ("mostly-silent", True, True),
    ("mostly-silent", False, False),
])
def test_route_requires_explicit_scene_or_manual_override(profile, override, expected):
    assert resolve_silence_skipping(profile, skip_non_speech=override) is expected


def test_route_default_is_off():
    assert resolve_silence_skipping() is False


@pytest.mark.parametrize("profile", ["auto", "noisy", "", None])
def test_unknown_scene_is_rejected(profile):
    with pytest.raises(ValueError, match="audio_profile"):
        resolve_silence_skipping(profile)


@pytest.mark.parametrize("override", ["false", "true", 0, 1, {}])
def test_non_boolean_override_is_rejected(override):
    with pytest.raises(ValueError, match="bool"):
        resolve_silence_skipping(skip_non_speech=override)


@pytest.mark.parametrize("kwargs", [{}, {"audio_profile": "general"},
                                 {"audio_profile": "mostly-silent", "skip_non_speech": False}])
def test_general_transcription_never_enters_compaction(monkeypatch, kwargs):
    audio = np.zeros(16000, dtype="float32")
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: audio)
    monkeypatch.setattr(
        qwen_transcriber, "_transcribe_with_silence_skipping",
        lambda *_: pytest.fail("Default transcription must retain all audio"),
    )

    def transcribe(**options):
        assert options["audio"][0] is audio
        return [SimpleNamespace(text="", time_stamps=None)]

    audit = {}
    result = qwen_transcriber.transcribe_qwen(
        SimpleNamespace(transcribe=transcribe), "source.wav", silence_report=audit, **kwargs,
    )
    assert result == ([], 1000)
    assert audit["routing"]["skip_non_speech"] is False


def test_user_confirmed_sparse_audio_routes_to_existing_compaction(monkeypatch):
    audio = np.zeros(16000, dtype="float32")
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: audio)
    calls = []

    def compact(*args):
        calls.append(args)
        return [], 1000

    monkeypatch.setattr(qwen_transcriber, "_transcribe_with_silence_skipping", compact)
    model, audit = object(), {}
    assert qwen_transcriber.transcribe_qwen(
        model, "source.wav", audio_profile="mostly-silent", silence_report=audit,
    ) == ([], 1000)
    assert calls[0][0] is model
    assert calls[0][1] is audio
    assert calls[0][3:7] == (90, 2, .3, 1.5)
    assert audit["routing"] == {"audio_profile": "mostly-silent", "skip_non_speech": True}


def test_invalid_scene_fails_before_decoding(monkeypatch):
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: pytest.fail("Must validate first"))
    with pytest.raises(ValueError, match="audio_profile"):
        qwen_transcriber.transcribe_qwen(object(), "source.wav", audio_profile="auto")


@pytest.mark.parametrize(("flags", "expected"), [
    ([], False),
    (["--audio-profile", "general"], False),
    (["--audio-profile", "mostly-silent"], True),
    (["--audio-profile", "mostly-silent", "--keep-non-speech"], False),
    (["--skip-non-speech"], True),
    (["--keep-non-speech"], False),
])
def test_cli_route_and_existing_chunk_defaults(flags, expected):
    args = qwen_chunked._parse_args(["source.mp4", "--output", "output", *flags])
    assert args.skip_non_speech is expected
    assert (args.chunk_seconds, args.batch_size, args.boundary_mode) == (300, 1, "fixed")


@pytest.mark.parametrize("flags", [
    ["--audio-profile", "auto"],
    ["--skip-non-speech", "--keep-non-speech"],
])
def test_cli_rejects_invalid_or_conflicting_choices(flags):
    with pytest.raises(SystemExit):
        qwen_chunked._parse_args(["source.mp4", "--output", "output", *flags])


@pytest.mark.parametrize(("profile", "condition"), [
    ("general", "no-skip"), ("mostly-silent", "skip"),
])
def test_staged_cli_passes_route_without_loading_any_model(monkeypatch, profile, condition):
    calls = []
    monkeypatch.setattr(sys, "argv", [
        "qwen_chunked.py", "source.mp4", "--output", "output",
        "--boundary-mode", "firered", "--chunk-seconds", "90", "--batch-size", "2",
        "--audio-profile", profile, "--staged",
    ])
    import qwen_resilience
    monkeypatch.setattr(qwen_resilience, "run_qwen",
                        lambda *_args, **kwargs: calls.append(kwargs["options"]) or ([], 1000))
    monkeypatch.setattr(qwen_chunked, "_write_result", lambda *_args: None)
    assert qwen_chunked.main() == 0
    assert calls[0].audio_profile == profile
    assert calls[0].skip_non_speech is (condition == "skip")
    assert (calls[0].chunk_seconds, calls[0].batch_size) == (90, 2)


def test_worker_forwards_profile_through_existing_small_interface(tmp_path):
    worker = QwenWorker.__new__(QwenWorker)
    requests = []
    worker._request = lambda request, *_: requests.append(request) or {
        "segments": [], "duration_ms": 2000,
    }
    options = {"audio_profile": "mostly-silent", "chunk_seconds": 90, "batch_size": 2}
    assert worker.transcribe(
        "source.wav", {"recognizer": tmp_path, "aligner": tmp_path}, None,
        silence_options=options, audit_output=tmp_path / "audit.json",
    ) == ([], 2000)
    assert requests[0]["silence_options"] == options
    assert requests[0]["silence_audit_output"] == str(tmp_path / "audit.json")


def test_staged_preparation_records_scene_and_keeps_original_samples(monkeypatch, tmp_path):
    import json

    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(b"\0\0" * 32000)
    monkeypatch.setattr(benchmark_qwen_silence, "detect_speech", lambda *_: {
        "speech_intervals": [[.5, 1]],
        "worker_wall_seconds": 0, "load_seconds": 0, "inference_seconds": 0,
    })
    output = tmp_path / "output"
    args = SimpleNamespace(
        video=tmp_path / "source.mp4", output=output, audio=source,
        sample_start=0, sample_seconds=None, chunk_seconds=90, batch_size=2,
        speech_padding_seconds=.3, min_removed_seconds=1.5,
        condition="no-skip", audio_profile="general", prepare_only=True,
    )
    assert benchmark_qwen_silence.run_trial(args) == 0
    payload = json.loads((output / "inference_plan.json").read_text(encoding="utf-8"))
    assert payload["routing"] == {"audio_profile": "general", "skip_non_speech": False}
    assert payload["sample_silence_plans"]["no-skip"]["removed_samples"] == 0
    assert sum(c["input_samples"] for c in payload["modes"][0]["chunks"]) == 32000
