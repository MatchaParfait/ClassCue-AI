import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest

import classcue.asr_models as asr_models
from classcue.preparation_progress import preparation_events
from scripts import prepare_asr_modelscope as downloader


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(downloader, "ROOT", tmp_path / "app")
    content = {"config.json": b"{}", "model.safetensors": b"weights",
               "tokenizer.json": b"{}", "README.md": b"not needed"}
    metadata = [
        {"Path": name, "Type": "blob", "Size": len(value),
         "Sha256": hashlib.sha256(value).hexdigest(), "Revision": "a" * 40}
        for name, value in content.items()
    ]
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"Code": 200, "Data": {"Files": metadata}})

    def stream(_method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        return nullcontext(httpx.Response(200, content=content[name], request=httpx.Request("GET", url)))

    monkeypatch.setattr(downloader.httpx, "get", get)
    monkeypatch.setattr(downloader.httpx, "stream", stream)
    return content, metadata, calls


@pytest.mark.parametrize("model_id", ["whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b"])
def test_verified_same_repository_bundle(source, model_id):
    _content, _metadata, calls = source
    progress = []
    downloader.prepare_model(model_id, lambda value, message: progress.append((value, message)))
    assert asr_models.model_is_downloaded(model_id)
    directory = asr_models.model_directory(model_id)
    manifest = json.loads((directory / "ready.json").read_text())
    assert manifest["download_source"] == "modelscope"
    assert set(manifest["sha256"]) == set(manifest["files"])
    for _component, repository in asr_models.get_model(model_id).repositories:
        assert any(repository in url for url in calls)
    assert not list(directory.rglob("*.part"))
    assert not list(directory.rglob("README.md"))
    assert progress[-1][0] == 1


def test_hash_mismatch_never_marks_ready(source):
    _content, metadata, _calls = source
    metadata[1]["Sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        downloader.prepare_model("whisper-turbo")
    directory = asr_models.model_directory("whisper-turbo")
    assert not (directory / "ready.json").exists()
    assert not list(directory.rglob("model.safetensors.part"))


def test_retry_reuses_verified_files_without_downloading(source, monkeypatch):
    downloader.prepare_model("qwen3-asr-1.7b")
    monkeypatch.setattr(downloader.httpx, "stream",
                        lambda *_a, **_kw: pytest.fail("Verified files must be reused"))
    downloader.prepare_model("qwen3-asr-1.7b")
    assert asr_models.model_is_downloaded("qwen3-asr-1.7b")


@pytest.mark.parametrize("name", ["../../outside.bin", "../other.bin", "C:/outside.bin"])
def test_untrusted_metadata_cannot_escape_component(source, name):
    _content, metadata, _calls = source
    metadata[1]["Path"] = name
    with pytest.raises(ValueError):
        downloader.prepare_model("whisper-turbo")
    assert not asr_models.model_directory("whisper-turbo").exists()


def test_missing_shard_rejects_bundle(source):
    content, metadata, _calls = source
    name = "model.safetensors.index.json"
    content[name] = json.dumps({"weight_map": {"tensor": "missing.safetensors"}}).encode()
    metadata.append({"Path": name, "Type": "blob", "Size": len(content[name]),
                     "Sha256": hashlib.sha256(content[name]).hexdigest(), "Revision": "b" * 40})
    with pytest.raises(RuntimeError, match="shard is missing"):
        downloader.prepare_model("qwen3-asr-1.7b")
    assert not asr_models.model_is_downloaded("qwen3-asr-1.7b")


def test_stream_resumes_partial_file(tmp_path, monkeypatch):
    partial = tmp_path / "weights.part"
    partial.write_bytes(b"ab")
    calls = []

    def stream(method, url, **kwargs):
        calls.append(kwargs["headers"])
        return nullcontext(httpx.Response(206, headers={"Content-Range": "bytes 2-5/6"}, content=b"cdef",
                                          request=httpx.Request(method, url)))

    monkeypatch.setattr(downloader.httpx, "stream", stream)
    downloader._stream("https://example.test/model", partial, 6, lambda _size: None)
    assert calls[0]["Range"] == "bytes=2-"
    assert partial.read_bytes() == b"abcdef"


def test_server_ignoring_range_restarts_file(tmp_path, monkeypatch):
    partial = tmp_path / "weights.part"
    partial.write_bytes(b"ab")
    monkeypatch.setattr(downloader.httpx, "stream", lambda method, url, **_kw:
                        nullcontext(httpx.Response(200, content=b"abcdef", request=httpx.Request(method, url))))
    downloader._stream("https://example.test/model", partial, 6, lambda _size: None)
    assert partial.read_bytes() == b"abcdef"


def test_stream_retries_disconnect_with_new_range(tmp_path, monkeypatch):
    partial = tmp_path / "weights.part"
    attempts = []

    def stream(method, url, **kwargs):
        attempts.append(kwargs["headers"].get("Range"))
        if len(attempts) == 1:
            partial.write_bytes(b"ab")
            raise httpx.ReadError("disconnected")
        return nullcontext(httpx.Response(206, headers={"Content-Range": "bytes 2-5/6"}, content=b"cdef",
                                          request=httpx.Request(method, url)))

    monkeypatch.setattr(downloader.httpx, "stream", stream)
    monkeypatch.setattr(downloader.time, "sleep", lambda _delay: None)
    downloader._stream("https://example.test/model", partial, 6, lambda _size: None)
    assert attempts == [None, "bytes=2-"]
    assert partial.read_bytes() == b"abcdef"


def test_empty_repository_file_can_be_verified(source):
    content, metadata, _calls = source
    content["merges.txt"] = b""
    metadata.append({"Path": "merges.txt", "Type": "blob", "Size": 0,
                     "Sha256": hashlib.sha256(b"").hexdigest(), "Revision": "a" * 40})
    downloader.prepare_model("whisper-turbo")
    assert asr_models.model_is_downloaded("whisper-turbo")


def test_aria_uses_partial_file_and_digest_before_atomic_publish(source, tmp_path, monkeypatch):
    aria = tmp_path / "app/.runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    aria.parent.mkdir(parents=True)
    aria.touch()
    content = b"x" * (1024 * 1024)
    destination = asr_models.component_paths("whisper-turbo")["recognizer"] / "model.bin"
    item = {"Size": len(content), "Sha256": hashlib.sha256(content).hexdigest()}
    commands = []

    def run(command, report, size):
        commands.append(command)
        destination.with_name("model.bin.part").write_bytes(content)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(downloader, "_aria_download", run)
    downloader._download(destination, item, "https://example.test/model", lambda _size: None)
    assert destination.read_bytes() == content
    assert "--out=model.bin.part" in commands[0]
    assert not destination.with_name("model.bin.part").exists()


def test_aria_failure_exposes_network_diagnostic_without_ready_marker(source, tmp_path, monkeypatch):
    aria = tmp_path / "app/.runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    aria.parent.mkdir(parents=True)
    aria.touch()
    destination = asr_models.component_paths("whisper-turbo")["recognizer"] / "model.bin"
    item = {"Size": 1024 * 1024, "Sha256": "0" * 64}
    def failure(*_a, **_kw):
        raise RuntimeError("HTTP response 403 Forbidden")
    monkeypatch.setattr(downloader, "_aria_download", failure)
    with pytest.raises(RuntimeError, match="403 Forbidden"):
        downloader._download(destination, item, "https://example.test/model", lambda _size: None)
    assert not destination.exists()


@pytest.mark.parametrize("model_id", [
    "whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b", "fireredasr2-aed",
    "paraformer-large-cpu", "dolphin-small-cn", "fun-asr-nano-2512",
])
def test_all_engines_cold_cache_reuse_and_deleted_cache_report_files(tmp_path, monkeypatch, model_id):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(downloader, "ROOT", tmp_path / "app")
    model = asr_models.get_model(model_id)
    required = {"firered": asr_models.FIRERED_REQUIRED_FILES, "paraformer": asr_models.PARAFORMER_REQUIRED_FILES,
                "dolphin": asr_models.DOLPHIN_REQUIRED_FILES, "nano": asr_models.NANO_REQUIRED_FILES}.get(model.engine)
    metadata = {}
    content = {}
    for component, repository in model.repositories:
        names = required[component] if required else ["config.json", "model.safetensors"]
        metadata[repository] = []
        for name in names:
            data = b"{}" if name == "config.json" else f"{component}/{name}".encode()
            content[(repository, name)] = data
            metadata[repository].append({"Path": name, "Type": "blob", "Size": len(data),
                "Sha256": hashlib.sha256(data).hexdigest(), "Revision": "a" * 40})
    def get(url, **kwargs):
        repository = url.split("/models/", 1)[1].split("/repo/files", 1)[0]
        if model.engine == "paraformer" or (model.engine in {"dolphin", "nano"} and "fsmn" in repository):
            assert kwargs["params"]["Revision"] == "v2.0.4"
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"Code": 200, "Data": {"Files": metadata[repository]}})
    calls = []
    def stream(_method, url, **_kwargs):
        calls.append(url)
        repository, name = url.split("/models/", 1)[1].split("/resolve/" + "a" * 40 + "/")
        return nullcontext(httpx.Response(200, content=content[(repository, name)],
                                         request=httpx.Request("GET", url)))
    monkeypatch.setattr(downloader.httpx, "get", get)
    monkeypatch.setattr(downloader.httpx, "stream", stream)
    events = []
    with preparation_events(events.append):
        manifest = downloader.download_components(model_id)
    assert len(manifest["files"]) == len(content)
    download_events = [event for event in events if event["phase"] == "downloading"]
    assert len({(event["component"], event["current_file"]) for event in download_events}) == len(content)
    assert download_events[-1]["bytes_completed"] == sum(map(len, content.values()))
    assert any(event["phase"] == "verifying" for event in events)
    calls.clear()
    downloader.download_components(model_id)
    assert calls == []  # Retry reuses SHA-256 verified files.
    assert asr_models.delete_model(model_id) > 0
    downloader.download_components(model_id)
    assert len(calls) == len(content)  # Deletion follows the same observable cold path.
