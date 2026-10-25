import json

import pytest

from classcue.asr_storage import slide_directory, storage_root


def test_storage_defaults_to_project_without_creating_files(tmp_path):
    assert storage_root(tmp_path) == tmp_path
    assert not (tmp_path / "data").exists()


def library_with_identity(tmp_path, **extra_identity):
    project = tmp_path / "project"
    configuration = project / "data" / "asr_storage.json"
    configuration.parent.mkdir(parents=True)
    identity = {"database": str(tmp_path / "library" / "project.db"), **extra_identity}
    configuration.write_text(json.dumps({"library_identity": identity}))
    return project


def test_pinned_slides_root_follows_only_its_relocated_library(tmp_path):
    pinned = tmp_path / "old-library" / "slides"
    project = library_with_identity(tmp_path, id="0123456789ab", slides_dir=str(pinned))
    assert slide_directory(project, tmp_path / "library") == pinned
    assert slide_directory(project, tmp_path / "isolated-test") == tmp_path / "isolated-test" / "slides"


def test_matching_library_without_pin_keeps_default_slides(tmp_path):
    project = library_with_identity(tmp_path, id="0123456789ab")
    assert slide_directory(project, tmp_path / "library") == tmp_path / "library" / "slides"


def test_bound_media_root_takes_priority_over_pinned_slides(tmp_path):
    pinned = tmp_path / "old-library" / "slides"
    project = library_with_identity(tmp_path, id="0123456789ab", slides_dir=str(pinned))
    configuration = project / "data" / "asr_storage.json"
    settings = json.loads(configuration.read_text())
    settings["media_storage_root"] = str(tmp_path / "media")
    configuration.write_text(json.dumps(settings))
    assert slide_directory(project, tmp_path / "library") == tmp_path / "media" / "slides"


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
