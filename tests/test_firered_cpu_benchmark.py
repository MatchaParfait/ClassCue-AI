import csv
import json

import pytest

from scripts.benchmark_firered_cpu import cpu_percent, summarize_resources, write_report


def test_cpu_percent_uses_all_logical_cores():
    assert cpu_percent(12, 1, 24) == 50
    assert cpu_percent(24, 1, 24) == 100
    assert cpu_percent(0, 0, 24) == 0


def test_resource_summary_weights_cpu_by_interval():
    samples = [
        {"sample_seconds": 1, "process_cpu_percent_all_logical_cores": 10,
         "system_cpu_percent": 20, "process_working_mib": 100,
         "system_available_physical_mib": 300},
        {"sample_seconds": 3, "process_cpu_percent_all_logical_cores": 50,
         "system_cpu_percent": 60, "process_working_mib": 200,
         "system_available_physical_mib": 200},
    ]
    result = summarize_resources(samples)
    assert result["samples"] == 2
    assert result["mean_process_cpu_percent_all_logical_cores"] == pytest.approx(40)
    assert result["mean_system_cpu_percent"] == pytest.approx(50)
    assert result["peak_process_working_mib"] == 200
    assert result["min_system_available_physical_mib"] == 200
    assert result["peak_process_private_commit_mib"] is None


def test_empty_resource_summary_is_defined():
    result = summarize_resources([])
    assert result["samples"] == 0
    assert result["mean_system_cpu_percent"] is None


def test_report_exports_cpu_and_memory_without_requiring_gpu(tmp_path):
    result = {
        "status": "failed", "video": "video.mp4", "started_at_utc": "2026-10-11T00:00:00+00:00",
        "batch_size": 3, "error": "memory guard", "resources_by_phase": {},
    }
    (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    sample = {
        "elapsed_seconds": 1.5, "phase": "aed", "sample_seconds": 0.5,
        "process_cpu_percent_all_logical_cores": 60, "system_cpu_percent": 80,
        "process_working_mib": 5500, "process_private_commit_mib": 7200,
    }
    (tmp_path / "resource_samples.jsonl").write_text(json.dumps(sample) + "\n", encoding="utf-8")
    write_report(tmp_path)
    with (tmp_path / "resources.csv").open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["process_cpu_percent_all_logical_cores"] == "60"
    assert rows[0]["process_working_mib"] == "5500"
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "memory guard" in report
    assert "no NVIDIA monitoring" in report
    assert "private commit is not physical RAM" in report
