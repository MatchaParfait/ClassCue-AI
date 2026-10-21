import os

import pytest

from classcue.qwen_memory import ensure_memory_headroom, memory_snapshot
import classcue.transcriber as transcriber


def test_safety_guard_checks_both_physical_and_commit_headroom():
    ensure_memory_headroom({"system_available_physical_mib": 3000, "system_available_commit_mib": 5000})
    for values in (
        {"system_available_physical_mib": 1500, "system_available_commit_mib": 5000},
        {"system_available_physical_mib": 3000, "system_available_commit_mib": 2000},
    ):
        with pytest.raises(RuntimeError, match="Insufficient memory"):
            ensure_memory_headroom(values)


@pytest.mark.skipif(os.name != "nt", reason="Windows memory counters")
def test_windows_snapshot_has_valid_process_and_system_counters():
    result = memory_snapshot()
    assert result["process_working_mib"] > 0
    assert result["process_private_commit_mib"] > 0
    assert result["system_total_physical_mib"] > result["system_available_physical_mib"] > 0
    assert result["system_commit_limit_mib"] > result["system_commit_mib"] > 0


def test_qwen_worker_forwards_opt_in_options_and_audit_path(tmp_path):
    worker = transcriber.QwenWorker.__new__(transcriber.QwenWorker)
    calls = []

    def request(payload, progress, should_stop):
        calls.append(payload)
        return {"segments": [], "duration_ms": 1234}

    worker._request = request
    audit = tmp_path / "audit.json"
    result = worker.transcribe(
        tmp_path / "input.wav", {"recognizer": "asr", "aligner": "align"}, None,
        silence_options={"skip_non_speech": True, "batch_size": 2}, audit_output=audit,
    )
    assert result == ([], 1234)
    assert calls[0]["silence_options"]["skip_non_speech"] is True
    assert calls[0]["silence_audit_output"] == str(audit.resolve())
