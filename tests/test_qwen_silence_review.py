from dataclasses import asdict
import json
import wave

import numpy as np

from qwen_boundaries import AudioSpan
from qwen_silence import build_silence_plan
from scripts.review_qwen_silence import build_review


def test_review_records_exact_deleted_audio_and_original_time_context(tmp_path):
    rate, total = 16000, 20 * 16000
    audio = np.arange(total, dtype=np.int16)
    source = tmp_path / "timeline.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(audio.tobytes())
    spans = [AudioSpan(0, total, "tail")]
    silence = build_silence_plan(spans, total, rate, [[0, 2], [10, 20]], enabled=True,
                                 padding_seconds=.3, min_removed_seconds=1.5)
    meta = {
        "video": "test.mp4", "source_audio": str(source), "sample_rate": rate,
        "full_audio_seconds": 20, "sample_start_seconds": 0, "sample_audio_seconds": 20,
    }
    (tmp_path / "inference_plan.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / "silence_full_plan.json").write_text(
        json.dumps({"silence": silence, "spans": [asdict(s) for s in spans]}), encoding="utf-8",
    )
    entries = build_review(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["start_seconds"] == 2.3
    assert entry["end_seconds"] == 9.7
    with wave.open(str(tmp_path / "review" / entry["deleted_audio"]), "rb") as deleted:
        removed = np.frombuffer(deleted.readframes(deleted.getnframes()), dtype="<i2")
    np.testing.assert_array_equal(removed, audio[round(2.3 * rate):round(9.7 * rate)])
    markup = (tmp_path / "review" / "index.html").read_text(encoding="utf-8")
    assert "__DATA__" not in markup and "__SUMMARY__" not in markup
    assert (tmp_path / "review" / "deleted_regions.csv").is_file()
