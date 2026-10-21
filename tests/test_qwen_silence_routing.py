from types import SimpleNamespace

import numpy as np
import pytest

import classcue.qwen_transcriber as qwen_transcriber
from classcue.qwen_silence import resolve_silence_skipping
from classcue.transcriber import QwenWorker


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
