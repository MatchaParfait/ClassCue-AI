import json

import pytest

from classcue.asr_storage import storage_root


def test_storage_defaults_to_project_without_creating_files(tmp_path):
    assert storage_root(tmp_path) == tmp_path
    assert not (tmp_path / "data").exists()


def test_machine_local_storage_override(tmp_path):
    configuration = tmp_path / "project" / "data" / "asr_storage.json"
    configuration.parent.mkdir(parents=True)
    target = tmp_path / "external" / "classroom-agent"
    configuration.write_text(json.dumps({"storage_root": str(target)}))
    assert storage_root(configuration.parent.parent) == target
    assert not target.exists()


@pytest.mark.parametrize("value", ["relative/path", "", None])
def test_invalid_storage_is_rejected(tmp_path, value):
    configuration = tmp_path / "data" / "asr_storage.json"
    configuration.parent.mkdir()
    configuration.write_text(json.dumps({"storage_root": value}))
    with pytest.raises(ValueError, match="storage configuration"):
        storage_root(tmp_path)
