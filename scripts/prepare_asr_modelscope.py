"""Verified, byte-reported ModelScope component downloads for every ASR engine."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import socket
import tempfile
import time
import uuid
from urllib.parse import quote

import httpx

from asr_models import (
    HUB_ALLOW_PATTERNS, ROOT, _managed_path, component_paths, get_model,
    model_directory, model_operation,
)
from preparation_progress import DownloadProgress, stage, checkpoint


def _verified(path: Path, item: dict, tracker=None) -> bool:
    if not path.is_file() or path.stat().st_size != item["Size"]:
        return False
    digest = hashlib.sha256()
    checked = 0
    last = 0
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            checkpoint()
            digest.update(chunk)
            checked += len(chunk)
            if tracker and time.monotonic() - last >= .5:
                tracker.verifying(path.name, checked=checked, total=item["Size"])
                last = time.monotonic()
    return digest.hexdigest() == item["Sha256"]


def _stream(url: str, partial: Path, size: int, report) -> None:
    for attempt in range(3):
        checkpoint()
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            partial.touch(exist_ok=True)
            return
        if offset > size:
            partial.unlink()
            offset = 0
        report(offset)
        try:
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                              timeout=httpx.Timeout(60, connect=10)) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise RuntimeError("Invalid download resume range")
                else:
                    offset = 0
                last_report = 0.0
                with partial.open("ab" if offset else "wb") as target:
                    for chunk in response.iter_bytes(64 * 1024):
                        checkpoint()
                        target.write(chunk)
                        offset += len(chunk)
                        if offset > size:
                            raise RuntimeError("Download exceeds expected file size")
                        if time.monotonic() - last_report >= 1:
                            report(offset)
                            last_report = time.monotonic()
            if offset != size:
                raise OSError(f"Incomplete download: {offset}/{size} bytes")
            return
        except (httpx.TransportError, OSError):
            if attempt == 2:
                raise
            stage("downloading", f"连接中断，正在重试 {partial.name.removesuffix('.part')}（{attempt + 1}/2）",
                  current_file=partial.name.removesuffix(".part"), retry_attempt=attempt + 1)
            time.sleep(attempt + 1)


def _aria_download(command, report, size, *, timeout=7200):
    # RPC provides completedLength; sparse .part length is not downloaded byte count.
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    secret = uuid.uuid4().hex
    command = command + ["--enable-rpc=true", "--rpc-listen-all=false",
                         f"--rpc-listen-port={port}", f"--rpc-secret={secret}"]
    with tempfile.TemporaryFile() as log, httpx.Client(trust_env=False, timeout=.5) as client:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW | 4) if os.name == "nt" else 0)
        owned_job = None
        started = time.monotonic()
        last_rpc = started
        last_size = 0
        try:
            if os.name == "nt":
                from qwen_process import WindowsJob
                # A backend crash must not leave an orphan writing this cache
                # while the restarted backend attempts to continue the file.
                owned_job = WindowsJob(process)
                owned_job.resume(process)
            while True:
                code = process.poll()
                if code is not None:
                    if code:
                        log.seek(0)
                        raise RuntimeError(f"ModelScope download failed: {log.read()[-3000:].decode('utf-8', errors='replace')}")
                    report(size)
                    return
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired("aria2c", timeout)
                try:
                    response = client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                        "jsonrpc": "2.0", "id": "progress", "method": "aria2.tellActive",
                        "params": [f"token:{secret}", ["completedLength", "totalLength"]],
                    })
                    payload = response.json()
                    active = payload.get("result", [])
                    if response.is_success and "result" in payload:
                        last_rpc = time.monotonic()
                    if isinstance(active, list) and active:
                        last_size = max(0, min(size, int(active[0]["completedLength"])))
                    elif isinstance(active, list):
                        stopped = client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                            "jsonrpc": "2.0", "id": "stopped", "method": "aria2.tellStopped",
                            "params": [f"token:{secret}", 0, 1, ["status", "errorMessage"]],
                        }).json().get("result", [])
                        if stopped and stopped[0].get("status") == "error":
                            raise RuntimeError(f"ModelScope download failed: {stopped[0].get('errorMessage', 'transfer error')}")
                        if stopped and stopped[0].get("status") == "complete":
                            report(size)
                            client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                                "jsonrpc": "2.0", "id": "stop", "method": "aria2.shutdown",
                                "params": [f"token:{secret}"],
                            })
                            process.wait(timeout=10)
                            return
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass  # Startup and checksum phases may have no active transfer.
                if time.monotonic() - last_rpc > 20:
                    raise RuntimeError("下载进度通道未响应，请重试模型准备")
                report(last_size)
                time.sleep(.5)
        finally:
            primary_error = sys.exc_info()[0] is not None
            try:
                if process.poll() is None:
                    # Graceful shutdown writes aria2's piece map. Killing first
                    # can turn a sparse file into an unsafe sequential resume.
                    try:
                        client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                            "jsonrpc": "2.0", "id": "save-pieces", "method": "aria2.forcePauseAll",
                            "params": [f"token:{secret}"],
                        })
                        client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                            "jsonrpc": "2.0", "id": "save-stop", "method": "aria2.shutdown",
                            "params": [f"token:{secret}"],
                        })
                        process.wait(timeout=8)
                    except (httpx.HTTPError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait()
            finally:
                if owned_job:
                    try:
                        owned_job.close()
                    except Exception:
                        if not primary_error:
                            raise


def _download(path: Path, item: dict, url: str, report, tracker=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _verified(path, item, tracker):
        report(item["Size"])
        return
    partial = _managed_path(path.with_name(path.name + ".part"))
    _managed_path(partial.with_name(partial.name + ".aria2"))
    record = _managed_path(partial.with_name(partial.name + ".json"))
    aria = ROOT / ".runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    segmented = aria.is_file() and item["Size"] >= 1024 * 1024
    try:
        previous = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if (previous and (previous.get("sha256") != item["Sha256"] or previous.get("size") != item["Size"])
            or (previous.get("segmented") and partial.exists()
                and not partial.with_name(partial.name + ".aria2").exists()
                and not _verified(partial, item, tracker))):
        partial.unlink(missing_ok=True)
        partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
    record.write_text(json.dumps({"sha256": item["Sha256"], "size": item["Size"],
                                 "segmented": segmented}), encoding="utf-8")
    if segmented:
        _aria_download([
            str(aria), f"--dir={path.parent}", f"--out={partial.name}",
            "--continue=true", "--allow-overwrite=true", "--auto-file-renaming=false",
            "--max-connection-per-server=8", "--split=8", "--min-split-size=8M",
            "--file-allocation=none", "--summary-interval=30",
            "--auto-save-interval=1",
            "--console-log-level=warn", "--show-console-readout=false",
            "--connect-timeout=10", "--timeout=60", "--max-tries=3", "--retry-wait=2",
            url,
        ], report, item["Size"])
    else:
        if partial.with_name(partial.name + ".aria2").exists():
            # A sequential Range request cannot safely reuse a segmented file
            # when aria2 is unavailable. Preserve other verified files.
            partial.unlink(missing_ok=True)
            partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
        _stream(url, partial, item["Size"], report)
    if not _verified(partial, item, tracker):
        partial.unlink(missing_ok=True)
        partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch: {path.name}")
    partial.replace(path)
    partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
    record.unlink(missing_ok=True)
    report(item["Size"])


def _validate_component(destination: Path) -> None:
    if not (destination / "config.json").is_file():
        raise RuntimeError("Model repository is missing config.json")
    if not any(destination.glob("*.bin")) and not any(destination.glob("*.safetensors")):
        raise RuntimeError("Model repository is missing weights")
    for index in destination.glob("*.safetensors.index.json"):
        mapping = json.loads(index.read_text(encoding="utf-8")).get("weight_map")
        if not isinstance(mapping, dict) or not mapping:
            raise RuntimeError("Model shard index is empty")
        for name in set(mapping.values()):
            shard = _managed_path(destination / name)
            if not shard.is_relative_to(destination) or not shard.is_file() or not shard.stat().st_size:
                raise RuntimeError(f"Model shard is missing: {name}")


def download_components(model_id: str, progress=None) -> dict:
    """Download the same pinned repositories for every engine; callers validate formats."""
    model = get_model(model_id)
    paths = component_paths(model.id)
    from asr_models import FIRERED_REQUIRED_FILES, PARAFORMER_REQUIRED_FILES, DOLPHIN_REQUIRED_FILES, NANO_REQUIRED_FILES
    required = {"firered": FIRERED_REQUIRED_FILES, "paraformer": PARAFORMER_REQUIRED_FILES,
                "dolphin": DOLPHIN_REQUIRED_FILES, "nano": NANO_REQUIRED_FILES}.get(model.engine)
    metadata = {}
    for component, repository in model.repositories:
        revision = "v2.0.4" if model.engine == "paraformer" or (
            model.engine in {"nano", "dolphin"} and component == "vad") else "master"
        stage("metadata", f"获取 {repository} 文件清单", repository=repository, component=component)
        response = httpx.get(
            f"https://modelscope.cn/api/v1/models/{repository}/repo/files",
            params={"Revision": revision, "Recursive": "true"}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("Code") != 200:
            raise RuntimeError(f"ModelScope metadata unavailable: {repository}")
        files = {}
        for item in payload["Data"]["Files"]:
            name = item["Path"]
            wanted = (name in required[component] or
                      (model.engine == "dolphin" and component == "recognizer" and name in {"bpe.model", "feats_stats.npz"})) if required else any(
                          fnmatch.fnmatchcase(name, pattern) for pattern in HUB_ALLOW_PATTERNS)
            if item.get("Type") == "tree" or not wanted:
                continue
            file = _managed_path(paths[component] / name)
            if not file.is_relative_to(paths[component]):
                raise ValueError("ModelScope file escapes the component directory")
            if (not isinstance(item["Size"], int) or isinstance(item["Size"], bool) or item["Size"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", item["Sha256"])
                    or not re.fullmatch(r"[0-9a-f]{40}", item["Revision"])):
                raise ValueError("Invalid ModelScope file metadata")
            files[name] = item
        if (required and not set(required[component]).issubset(files)) or (
            not required and ("config.json" not in files or not any(
                name.endswith((".bin", ".safetensors")) for name in files))):
            raise RuntimeError(f"ModelScope repository is incomplete: {repository}")
        metadata[component] = files
    total = sum(item["Size"] for files in metadata.values() for item in files.values())
    tracker = DownloadProgress(total, progress)
    for component, repository in model.repositories:
        for name, item in metadata[component].items():
            tracker.file(component, repository, name, item["Size"])
            if component == "vad" and model.engine in {"dolphin", "nano"}:
                # The same official VAD can be reused, but only after digest validation.
                import shutil
                existing = component_paths("paraformer-large-cpu")["vad"] / name
                if _verified(existing, item, tracker):
                    destination = paths[component] / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(existing, destination)
                    tracker.report(item["Size"])
                    tracker.completed += item["Size"]
                    continue
            url = (f"https://modelscope.cn/models/{repository}/resolve/"
                   f"{quote(item['Revision'], safe='')}/{quote(name, safe='/')}")
            _download(paths[component] / name, item, url, tracker.report, tracker)
            tracker.completed += item["Size"]
        if not required:
            stage("verifying", f"检查 {component} 权重结构与分片", component=component)
            _validate_component(paths[component])
    manifest = {
        "repositories": dict(model.repositories),
        "files": {f"{component}/{name}": item["Size"]
                  for component, files in metadata.items() for name, item in files.items()},
        "sha256": {f"{component}/{name}": item["Sha256"]
                   for component, files in metadata.items() for name, item in files.items()},
        "download_source": "modelscope",
    }
    return manifest


def prepare_model(model_id: str, progress=None) -> None:
    model = get_model(model_id)
    if model.engine not in {"whisper", "qwen", "firered"}:
        raise ValueError("This entry point only accepts Whisper, Qwen and FireRed models")
    with model_operation(model.id):
        manifest = download_components(model.id, progress)
        directory = model_directory(model.id)
        stage("finalizing", "保存已校验的模型清单")
        temporary = directory / "ready.tmp"
        temporary.write_text(json.dumps(manifest), encoding="utf-8")
        temporary.replace(directory / "ready.json")
        if progress:
            progress(1.0, f"{model.label} ModelScope 下载和 SHA-256 校验完成")
