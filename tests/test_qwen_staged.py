import json

from scripts.benchmark_qwen_silence import merge_staged_results


def test_phase_merge_sums_time_not_memory_and_preserves_alignment(tmp_path):
    def metric(mode, stage):
        return {
            "mode": mode, "status": "completed", "wall_seconds": 10 if stage == "asr" else 20,
            "stage_seconds": 9 if stage == "asr" else 18, "audio_seconds": 90,
            "segment_count": 0 if stage == "asr" else 3,
            "mapping_warnings": [] if stage == "asr" else [{"reason": "test"}],
            "batch_memory": [{"process_private_commit_mib": 8000 if stage == "asr" else 6000}],
            "peak_process_working_mib": 2000 if stage == "asr" else 5000,
            "peak_process_private_commit_mib": 9000 if stage == "asr" else 7000,
            "min_system_available_physical_mib": 8000 if stage == "asr" else 5000,
            "min_system_available_commit_mib": 6000 if stage == "asr" else 8000,
        }
    for stage in ("asr", "align"):
        (tmp_path / f"{stage}_timings.json").write_text(json.dumps({
            "load_seconds": 4 if stage == "asr" else 2,
            "modes": [metric("no-skip", stage), metric("skip", stage)],
        }), encoding="utf-8")
        (tmp_path / f"cpu_memory_samples_{stage}.json").write_text(json.dumps({
            "baseline": {"system_available_physical_mib": 10000},
            "samples": [{"elapsed_seconds": 2, "process_private_commit_mib": 9000 if stage == "asr" else 7000}],
        }), encoding="utf-8")
    merge_staged_results(tmp_path)
    result = json.loads((tmp_path / "qwen_timings.json").read_text(encoding="utf-8"))
    item = result["modes"][0]
    assert result["load_seconds"] == 6
    assert item["wall_seconds"] == 30
    assert item["asr_seconds"] == 9
    assert item["align_seconds"] == 18
    assert item["peak_process_private_commit_mib"] == 9000
    assert item["peak_process_working_mib"] == 5000
    assert item["segment_count"] == 3
    samples = json.loads((tmp_path / "cpu_memory_samples.json").read_text(encoding="utf-8"))
    assert samples["stages_never_overlap"]
    assert [s["phase"] for s in samples["samples"]] == ["asr", "align"]
