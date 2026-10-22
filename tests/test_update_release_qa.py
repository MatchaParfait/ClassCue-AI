from pathlib import Path

import pytest

from tests.update_release_qa import checked_record, seed, snapshot


@pytest.mark.parametrize("external", [False, True])
def test_fixture_has_stable_metadata_and_preserves_user_state(tmp_path, external):
    root = tmp_path / ".runtime" / "install"
    root.mkdir(parents=True)
    record = seed(root, external=external)
    result = snapshot(record)
    video = result["rows"]["videos"][0]
    stat = (Path(record["media"]) / "videos/fixture.mp4").stat()
    assert video[8] == stat.st_size
    assert video[9] == stat.st_mtime_ns
    assert len(result["rows"]["video_markers"]) == 1
    assert len(result["rows"]["video_transcript_segments"]) == 1
    assert result == snapshot(record)


def test_arbitrary_installations_cannot_be_started_or_controlled(tmp_path):
    with pytest.raises(ValueError, match="isolated"):
        checked_record(tmp_path)
