from contextlib import nullcontext
import hashlib
import json
from types import SimpleNamespace

import pytest

import httpx
import classcue.asr_models as asr_models
from scripts import prepare_asr_modelscope as downloader


def prepare():
    downloader.prepare_model("fireredasr2-aed")


@pytest.fixture
def download_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path)
    root = tmp_path / "app"
    aria = root / ".runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    aria.parent.mkdir(parents=True)
    aria.write_bytes(b"aria fixture")
    directory = tmp_path / "model"
    destination = directory / "recognizer"
    content = {
        "model.pth.tar": b"w" * (1024 * 1024),
        "cmvn.ark": b"cmvn", "dict.txt": b"dict",
        "train_bpe1000.model": b"bpe", "config.yaml": b"",
    }
    files = [
        {"Path": name, "Size": len(data), "Sha256": hashlib.sha256(data).hexdigest(),
         "Revision": "a" * 40}
        for name, data in content.items()
    ]
    vad_content = {"VAD/model.pth.tar": b"vad", "VAD/cmvn.ark": b"vad-cmvn"}
    vad_files = [
        {"Path": name, "Size": len(data), "Sha256": hashlib.sha256(data).hexdigest(),
         "Revision": "a" * 40}
        for name, data in vad_content.items()
    ]
    monkeypatch.setattr(downloader, "ROOT", root)
    monkeypatch.setattr(downloader, "component_paths", lambda _id: {
        "recognizer": destination, "vad": directory / "vad",
    })
    monkeypatch.setattr(downloader, "model_directory", lambda _id: directory)
    monkeypatch.setattr(downloader, "model_operation", lambda _id: nullcontext())
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if "/api/v1/" in url:
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"Code": 200, "Data": {
                                       "Files": vad_files if "FireRedVAD/" in url else files,
                                   }})
        data = vad_content[url.split("a" * 40 + "/")[-1]] if "FireRedVAD/" in url else content[url.rsplit("/", 1)[-1]]
        return httpx.Response(200, content=data, request=httpx.Request("GET", url))

    class Response:
        def __init__(self, method, url, **kwargs):
            self.response = get(url, **kwargs)

        def __enter__(self):
            return self.response

        def __exit__(self, *_args):
            return False

    def run(command, report, size):
        calls.append(command[-1])
        (destination / "model.pth.tar.part").write_bytes(content["model.pth.tar"])

    monkeypatch.setattr(downloader.httpx, "get", get)
    monkeypatch.setattr(downloader.httpx, "stream", Response)
    monkeypatch.setattr(downloader, "_aria_download", run)
    return directory, content, calls, files, vad_files


def test_official_alternative_download_checks_hashes_and_marks_ready(download_fixture):
    directory, content, calls, _files, _vad_files = download_fixture
    prepare()
    manifest = json.loads((directory / "ready.json").read_text())
    assert len(manifest["files"]) == 7
    assert manifest["files"]["recognizer/config.yaml"] == 0
    assert manifest["download_source"] == "modelscope"
    assert all("/resolve/" + "a" * 40 + "/" in url for url in calls[2:])
    for name, data in content.items():
        assert (directory / "recognizer" / name).read_bytes() == data


def test_corrupted_download_never_marks_model_ready(download_fixture):
    directory, _content, _calls, files, _vad_files = download_fixture
    files[1]["Sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        prepare()
    assert not (directory / "ready.json").exists()


def test_incomplete_repository_is_rejected_before_download(download_fixture):
    directory, _content, _calls, files, _vad_files = download_fixture
    files.pop()
    with pytest.raises(RuntimeError, match="incomplete"):
        prepare()
    assert not directory.exists()


def test_second_preparation_reuses_all_verified_files(download_fixture):
    directory, _, calls, _, _ = download_fixture
    prepare()
    calls.clear()
    prepare()
    assert len(calls) == 2
    assert (directory / "ready.json").exists()


def test_missing_vad_cannot_mark_aed_only_cache_ready(download_fixture):
    directory, _, _, _, vad_files = download_fixture
    vad_files.pop()
    with pytest.raises(RuntimeError, match="incomplete"):
        prepare()
    assert not directory.exists()


def test_vad_hash_failure_never_marks_bundle_ready(download_fixture):
    directory, _, _, _, vad_files = download_fixture
    vad_files[0]["Sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        prepare()
    assert not (directory / "ready.json").exists()
