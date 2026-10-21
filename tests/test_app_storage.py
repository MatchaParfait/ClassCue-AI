import json

import pytest

import classcue.app_storage as app_storage
from api.main import StoreProvider


def test_default_is_a_single_installation_library(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_DATA_ROOT", raising=False)
    monkeypatch.setattr(app_storage, "ROOT", tmp_path)
    monkeypatch.setattr(app_storage, "DEFAULT_LIBRARY_ROOT", tmp_path / "data/library")
    assert app_storage.load_data_root() == tmp_path / "data/library"
    assert not (tmp_path / "data").exists()


def test_saved_data_directory_keeps_existing_library_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_DATA_ROOT", raising=False)
    monkeypatch.setattr(app_storage, "ROOT", tmp_path)
    existing = tmp_path / "existing-data"
    existing.mkdir()
    database = existing / "project.db"
    database.write_bytes(b"untouched")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/asr_storage.json").write_text(json.dumps({"library_data_dir": str(existing)}))
    assert app_storage.load_data_root() == existing
    assert database.read_bytes() == b"untouched"


def test_obsolete_workspace_config_and_environment_are_not_used(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_DATA_ROOT", raising=False)
    monkeypatch.setenv("CLASSROOM_WORKSPACE_ROOT", str(tmp_path / "obsolete"))
    monkeypatch.setattr(app_storage, "ROOT", tmp_path)
    monkeypatch.setattr(app_storage, "DEFAULT_LIBRARY_ROOT", tmp_path / "data/library")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/video_workspace.json").write_text(json.dumps({"workspace_root": str(tmp_path / "old")}))
    assert app_storage.load_data_root() == tmp_path / "data/library"


def test_provider_is_lazy_and_pins_one_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(tmp_path / "library"))
    provider = StoreProvider()
    assert not (tmp_path / "library").exists()
    first = provider.get()
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(tmp_path / "other"))
    assert provider.get() is first
    assert provider.data_root() == tmp_path / "library"
    assert not (tmp_path / "other").exists()


def test_invalid_data_configuration_does_not_silently_open_an_empty_library(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_DATA_ROOT", raising=False)
    monkeypatch.setattr(app_storage, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/asr_storage.json").write_text(json.dumps({"library_data_dir": "relative"}))
    with pytest.raises(ValueError):
        app_storage.load_data_root()
