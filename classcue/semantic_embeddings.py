"""Explicit preparation and bounded, reusable local embedding workers."""

import atexit
import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
from threading import Event, Lock, RLock, Thread, Timer
import time
from urllib.parse import quote

import numpy as np

from classcue.asr_storage import active_storage_root, log_directory
from classcue.desktop_runtime import atomic_json, read_json
from classcue.paths import PROJECT_ROOT
from classcue.processing_control import ProcessingCancelled, ResourcePaused


MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
DIMENSIONS = 1024
RUNTIME_VERSION = "qwen-embedding-torch271-transformers4576-v1"
_GPU_STATUS = None
WEIGHTS_DOWNLOAD_BYTES = 1_207_469_240
DEPENDENCIES_DOWNLOAD_BYTES = 80_000_000
TORCH_DOWNLOAD_BYTES = {"cpu": 215_985_616, "cuda": 3_273_024_349}
CLEANUP_SCOPES = {"cpu": "CPU 检索环境", "cuda": "GPU 检索环境",
                  "weights": "共享检索权重", "all": "全部 RAG 缓存"}


def _runtime_root():
    return active_storage_root(PROJECT_ROOT) / ".runtime"


def _selection_path():
    return _runtime_root() / "semantic-search-selection.json"


def _marker(path):
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def selected_device():
    saved = _marker(_selection_path())
    if saved.get("device") in {"cpu", "cuda"}:
        return saved["device"]
    legacy = _marker(_runtime_root() / "semantic-search" / "ready.json")
    return legacy["device"] if legacy.get("device") in {"cpu", "cuda"} else (
        "cuda" if _gpu_detection() else "cpu")


def _runtime_ready(directory, device, *, legacy=False):
    marker = _marker(directory / "ready.json")
    return bool(
        marker.get("version") == RUNTIME_VERSION and marker.get("device") == device
        and (marker.get("torch_variant") == device or (legacy and device == "cuda"))
        and (directory / "Scripts" / "python.exe").is_file()
    )


def runtime_directory(device=None):
    device = device or selected_device()
    if device not in {"cpu", "cuda"}:
        raise ValueError("检索设备必须为 cpu 或 cuda")
    directory = _runtime_root() / f"semantic-search-{device}"
    if _runtime_ready(directory, device):
        return directory
    legacy = _runtime_root() / "semantic-search"
    return legacy if _runtime_ready(legacy, device, legacy=True) else directory


def model_directory():
    return active_storage_root(PROJECT_ROOT) / "models" / "embeddings" / "qwen3-embedding-0.6b"


def runtime_python(device=None):
    return runtime_directory(device) / "Scripts" / "python.exe"


def _gpu_detection():
    global _GPU_STATUS
    if _GPU_STATUS and time.monotonic() - _GPU_STATUS[0] < 30:
        return _GPU_STATUS[1]
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        detected = result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        detected = False
    _GPU_STATUS = (time.monotonic(), detected)
    return detected


def _weights_status():
    marker = _marker(model_directory() / "ready.json")
    files = marker.get("files", {})
    if not isinstance(files, dict):
        files = {}
    try:
        ready = bool(files) and bool(marker.get("fingerprint")) and all(
            isinstance(name, str) and Path(name).name == name
            and type(size) is int and size >= 0
            and (model_directory() / name).is_file()
            and (model_directory() / name).stat().st_size == size for name, size in files.items()
        )
    except OSError:
        ready = False
    return ready, marker.get("fingerprint", ""), sum(size for size in files.values() if type(size) is int)


def model_status():
    weights_ready, fingerprint, size = _weights_status()
    device = selected_device()
    runtimes = {
        name: {"ready": _runtime_ready(runtime_directory(name), name,
                                      legacy=runtime_directory(name).name == "semantic-search")}
        for name in ("cpu", "cuda")
    }
    gpu = _gpu_detection()
    weights_download = 0 if weights_ready else WEIGHTS_DOWNLOAD_BYTES
    downloads = {
        name: 0 if runtimes[name]["ready"] else DEPENDENCIES_DOWNLOAD_BYTES + (
            0 if (_runtime_root() / "wheels" / (
                f"torch-2.7.1+{'cpu' if name == 'cpu' else 'cu128'}-cp312-cp312-win_amd64.whl"
            )).is_file() else TORCH_DOWNLOAD_BYTES[name])
        for name in ("cpu", "cuda")
    }
    return {"model_id": MODEL_ID, "dimensions": DIMENSIONS,
            "ready": weights_ready and runtimes[device]["ready"] and (device == "cpu" or gpu),
            "weights_ready": weights_ready, "runtimes": runtimes,
            "gpu_detected": gpu,
            "gpu_unavailable_reason": "" if gpu else "未检测到可用的 NVIDIA 显卡及驱动",
            "model_version": fingerprint, "device": device, "bytes": size,
            "download_estimates": {
                "weights_bytes": weights_download, "runtimes": downloads,
                "total_bytes": {name: weights_download + value for name, value in downloads.items()},
            },
            "cache_present": any(path.exists() or path.is_symlink() for path, _ in _cleanup_targets())}


def _legacy_device():
    legacy = _runtime_root() / "semantic-search"
    marker = legacy / "ready.json"
    owner = _runtime_root() / "semantic-search-legacy-device.json"
    for path in (legacy, marker, owner):
        if path.resolve() != path.absolute():
            raise ValueError("检索缓存不能经过符号链接或目录联接")
    for path in (marker, owner):
        device = _marker(path).get("device")
        if device in {"cpu", "cuda"}:
            return device
    # The original shared runtime shipped CUDA dependencies before device splitting.
    return "cuda"


def _runtime_targets(device):
    root = _runtime_root()
    targets = [(root / f"semantic-search-{device}", CLEANUP_SCOPES[device])]
    if _legacy_device() == device:
        targets.append((root / "semantic-search", CLEANUP_SCOPES[device]))
    return targets


def _cleanup_targets(target="all"):
    if target not in CLEANUP_SCOPES:
        raise ValueError("检索清理范围必须为 cpu、cuda、weights 或 all")
    if target == "weights":
        return [(model_directory(), CLEANUP_SCOPES["weights"])]
    if target in {"cpu", "cuda"}:
        return _runtime_targets(target)
    root = _runtime_root()
    return [(model_directory(), CLEANUP_SCOPES["weights"]),
            *[(root / name, "检索运行环境") for name in (
                "semantic-search", "semantic-search-cpu", "semantic-search-cuda")],
            (_selection_path(), "检索设备选择"),
            (_selection_path().with_suffix(".tmp"), "检索准备临时文件")]


def cache_inventory():
    """Count owned files on demand without taking the job lock or following links."""
    from classcue.cleanup_progress import CleanupProgress, _scan

    root = active_storage_root(PROJECT_ROOT).absolute()
    if root.resolve() != root:
        raise ValueError("检索缓存根目录不能经过符号链接或目录联接")

    def component(targets, ready):
        for path, _ in targets:
            if path.resolve() != path.absolute() or not path.is_relative_to(root):
                raise ValueError("检索缓存不能越出模型目录或经过符号链接")
        try:
            inventory = _scan(targets, CleanupProgress("检查检索缓存"))
        except FileNotFoundError:
            # A concurrent cleanup changed the tree; the next explicit refresh sees it.
            inventory = _scan([(path, label) for path, label in targets if path.exists()],
                              CleanupProgress("检查检索缓存"))
        return {"present": bool(inventory.files), "ready": ready(),
                "bytes": inventory.bytes_total, "files": len(inventory.files)}

    def runtime_ready(device):
        return any(_runtime_ready(path, device, legacy=path.name == "semantic-search")
                   for path, _ in _runtime_targets(device))

    return {
        "weights": component(_cleanup_targets("weights"), lambda: _weights_status()[0]),
        "runtimes": {device: component(_runtime_targets(device), lambda device=device: runtime_ready(device))
                     for device in ("cpu", "cuda")},
    }


def cleanup_model(should_stop=None, *, target="all", keep_device=None):
    """Remove only dedicated retrieval assets; keep shared downloads and course evidence."""
    from filelock import FileLock
    from classcue.cleanup_progress import cleanup_directories
    from classcue.preparation_progress import preparation_events

    root = active_storage_root(PROJECT_ROOT).absolute()
    runtime = _runtime_root().absolute()
    if root.resolve() != root or runtime.resolve() != runtime:
        raise ValueError("检索清理目录不能经过链接")
    _checkpoint(should_stop)
    runtime.mkdir(parents=True, exist_ok=True)

    def check():
        if should_stop and should_stop():
            raise ProcessingCancelled("已停止检索缓存清理")

    with encoder.lock, FileLock(str(runtime / "semantic-search-setup.lock"), timeout=0):
        if keep_device is not None:
            if keep_device not in {"cpu", "cuda"} or target != (
                    "cuda" if keep_device == "cpu" else "cpu"):
                raise ValueError("清理范围必须是保留设备之外的运行环境")
            state = model_status()
            if (not state["weights_ready"] or not state["runtimes"][keep_device]["ready"]
                    or (keep_device == "cuda" and not state["gpu_detected"])):
                raise RuntimeError(f"请先下载并准备 {'GPU' if keep_device == 'cuda' else 'CPU'}，再清理另一套环境")
            close_worker()
            _validate_runtime(keep_device, should_stop)
            _checkpoint(should_stop)
            atomic_json(_selection_path(), {"device": keep_device})
        close_worker()
        encoder.query_cache.clear()
        targets = _cleanup_targets(target)
        legacy = runtime / "semantic-search"
        owner = runtime / "semantic-search-legacy-device.json"
        owner_paths = (owner, owner.with_suffix(".tmp"))
        removes_legacy = any(path == legacy for path, _ in targets) and legacy.exists()
        if target == "all" or removes_legacy:
            if any(path.resolve() != path for path in owner_paths):
                raise ValueError("检索缓存元数据不能经过链接")
        if removes_legacy:
            atomic_json(owner, {"device": _legacy_device()})
        with preparation_events(None, checkpoint=check):
            released = cleanup_directories(
                root, targets, scope=CLEANUP_SCOPES[target])
        if (removes_legacy or target == "all") and not legacy.exists():
            if any(path.resolve() != path for path in owner_paths):
                raise ValueError("检索缓存元数据不能经过链接")
            for path in owner_paths:
                path.unlink(missing_ok=True)
        return released


def _environment(device=None):
    env = os.environ.copy()
    cache = active_storage_root(PROJECT_ROOT) / ".runtime" / "cache"
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", TOKENIZERS_PARALLELISM="false",
               HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_XET="1",
               HF_HOME=str(cache / "huggingface"), TORCH_HOME=str(cache / "torch"),
               CUDA_CACHE_PATH=str(cache / "cuda"))
    env.setdefault("UV_CACHE_DIR", str(cache / "uv"))
    env.setdefault("PIP_CACHE_DIR", str(cache / "pip"))
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def _checkpoint(should_stop):
    from classcue.preparation_progress import checkpoint
    checkpoint()
    if should_stop and should_stop():
        raise ProcessingCancelled("已停止检索模型准备")


def _uv_prefix():
    bundled = PROJECT_ROOT / ".runtime" / "bin" / "uv.exe"
    uv = shutil.which("uv")
    prefix = [str(bundled)] if bundled.is_file() else (
        [uv] if uv else [sys.executable, "-m", "uv"] if importlib.util.find_spec("uv") else None)
    if prefix is None:
        raise RuntimeError("准备课程检索需要 uv，请先使用应用启动器准备运行环境")
    return prefix


def _prepare_environment(device, directory, progress, should_stop):
    from classcue.preparation_progress import run_logged
    env = _environment(device)
    env.setdefault("UV_PYTHON_INSTALL_DIR", str(PROJECT_ROOT / ".runtime" / "python"))
    prefix = _uv_prefix()
    python = directory / "Scripts" / "python.exe"
    commands = []
    if not python.is_file():
        commands.append(prefix + ["venv", "--python", "3.12", str(directory)])
    pip = prefix + ["pip", "install", "--python", str(python)]
    variant = "cpu" if device == "cpu" else "cu128"
    wheel = _runtime_root() / "wheels" / f"torch-2.7.1+{variant}-cp312-cp312-win_amd64.whl"
    commands += [
        pip + [str(wheel) if wheel.is_file() else f"torch==2.7.1+{variant}",
               "--index-url", f"https://download.pytorch.org/whl/{variant}"],
        pip + ["transformers==4.57.6", "safetensors==0.8.0",
               "--index-url", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"],
    ]
    log_path = log_directory(PROJECT_ROOT) / "semantic-prepare.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        for index, command in enumerate(commands):
            _checkpoint(should_stop)
            if progress:
                progress(.1 * index / len(commands), f"正在准备 {'CPU' if device == 'cpu' else 'GPU'} 检索环境",
                         "preparing")
            result = run_logged(command, stdout=log, stderr=log, cwd=PROJECT_ROOT, env=env,
                                timeout=3600, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode and command is commands[-1]:
                fallback = [*command[:-1], "https://pypi.org/simple"]
                result = run_logged(fallback, stdout=log, stderr=log, cwd=PROJECT_ROOT, env=env,
                                    timeout=3600, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError("检索运行环境准备失败，请检查网络和可用磁盘空间后重试")


def _stream_weight(url, partial, size, report, should_stop):
    import httpx
    for attempt in range(3):
        _checkpoint(should_stop)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            return
        if offset > size:
            partial.unlink()
            offset = 0
        report(offset)
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with httpx.stream("GET", url, headers=headers, trust_env=False, follow_redirects=True,
                              timeout=httpx.Timeout(60, connect=10)) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("模型断点续传范围无效")
                else:
                    offset = 0
                last_report = 0
                with partial.open("ab" if offset else "wb") as target:
                    for chunk in response.iter_bytes(64 * 1024):
                        _checkpoint(should_stop)
                        target.write(chunk)
                        offset += len(chunk)
                        if offset > size:
                            raise ValueError("模型下载超过文件清单大小")
                        if time.monotonic() - last_report >= 1:
                            report(offset)
                            last_report = time.monotonic()
            if offset != size:
                raise OSError("模型文件下载未完成")
            return
        except (httpx.TransportError, httpx.HTTPStatusError, OSError) as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                raise
            if attempt == 2:
                raise RuntimeError("检索模型下载中断，请检查网络后重试，已下载部分会保留") from None
            time.sleep(attempt + 1)


def _parallel_weight(url, partial, size, report, should_stop):
    """Keep only a contiguous prefix on disk and at most eight chunks in memory."""
    import httpx

    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size:
        partial.unlink()
        offset = 0
    if offset == size:
        return
    block = 4 * 1024 * 1024
    starts = range(offset, size, block)
    received = dict.fromkeys(starts, 0)
    lock, stopped = Lock(), Event()

    def fetch(start):
        end = min(start + block, size) - 1
        for attempt in range(3):
            if stopped.is_set():
                return None
            with lock:
                received[start] = 0
            try:
                with client.stream("GET", url, headers={
                    "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
                }) as response:
                    response.raise_for_status()
                    if (response.status_code != 206 or response.headers.get("Content-Range")
                            != f"bytes {start}-{end}/{size}"):
                        raise ValueError("模型分段下载范围无效")
                    content = bytearray()
                    for chunk in response.iter_bytes(64 * 1024):
                        if stopped.is_set():
                            return None
                        if len(content) + len(chunk) > end - start + 1:
                            raise ValueError("模型分段下载超过文件清单大小")
                        content.extend(chunk)
                        with lock:
                            received[start] = len(content)
                    if len(content) != end - start + 1:
                        raise httpx.ReadError("模型分段下载未完成")
                    return content
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if (isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500
                        or attempt == 2):
                    raise
                if stopped.wait(attempt + 1):
                    return None

    report(offset)
    with httpx.Client(trust_env=False, follow_redirects=True,
                      timeout=httpx.Timeout(20, connect=10)) as client:
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="embedding-download")
        pending = OrderedDict()
        remaining = iter(starts)
        last_report = 0
        try:
            with partial.open("ab" if offset else "wb") as target:
                while pending or remaining is not None:
                    _checkpoint(should_stop)
                    while remaining is not None and len(pending) < 8:
                        start = next(remaining, None)
                        if start is None:
                            remaining = None
                        else:
                            pending[start] = pool.submit(fetch, start)
                    if not pending:
                        break
                    if time.monotonic() - last_report >= 1:
                        with lock:
                            completed = offset + sum(received.values())
                        report(completed)
                        last_report = time.monotonic()
                    start, future = next(iter(pending.items()))
                    try:
                        content = future.result(timeout=.2)
                    except FutureTimeout:
                        continue
                    _checkpoint(should_stop)
                    target.write(content)
                    del content
                    del future
                    del pending[start]
        finally:
            stopped.set()
            pool.shutdown(wait=True, cancel_futures=True)
            report(partial.stat().st_size if partial.exists() else 0)


def _download_weight(name, item, partial, tracker, should_stop):
    from scripts.prepare_asr_modelscope import _verified
    import httpx

    sources = [
        ("ModelScope", f"https://modelscope.cn/models/{MODEL_ID}/resolve/"
         f"{item['Revision']}/{quote(name)}"),
        ("HF 镜像", f"https://hf-mirror.com/{MODEL_ID}/resolve/main/{quote(name)}"),
    ]
    for index, (source, url) in enumerate(sources):
        _checkpoint(should_stop)
        tracker.source = source
        try:
            if item["Size"] >= 16 * 1024 * 1024:
                try:
                    _parallel_weight(url, partial, item["Size"], tracker.report, should_stop)
                except ValueError:
                    # Some mirrors ignore bounded ranges; sequential resume checks its own range.
                    _stream_weight(url, partial, item["Size"], tracker.report, should_stop)
            else:
                _stream_weight(url, partial, item["Size"], tracker.report, should_stop)
            if _verified(partial, item, tracker):
                return
            partial.unlink(missing_ok=True)
        except (ProcessingCancelled, ResourcePaused):
            raise
        except (httpx.HTTPError, RuntimeError, ValueError):
            if index == len(sources) - 1:
                raise RuntimeError(f"检索模型下载失败：{name}，请检查网络后重试") from None
        if index == len(sources) - 1:
            raise RuntimeError(f"检索模型文件校验失败：{name}")


def _prepare_weights(progress, should_stop):
    from classcue.preparation_progress import DownloadProgress, stage
    from scripts.prepare_asr_modelscope import _verified
    import httpx

    if _weights_status()[0]:
        return
    destination = model_directory()
    destination.mkdir(parents=True, exist_ok=True)
    stage("metadata", "获取 Qwen3-Embedding-0.6B 文件清单")
    response = httpx.get(f"https://modelscope.cn/api/v1/models/{MODEL_ID}/repo/files",
                         params={"Revision": "master", "Recursive": "true"}, timeout=30,
                         trust_env=False)
    response.raise_for_status()
    wanted = {"config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
              "vocab.json", "merges.txt", "special_tokens_map.json"}
    files = {item["Path"]: item for item in response.json()["Data"]["Files"]
             if item.get("Path") in wanted and item.get("Type") != "tree"}
    if not {"config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json"}.issubset(files):
        raise RuntimeError("Qwen 检索模型的官方文件清单不完整")
    for item in files.values():
        if (type(item.get("Size")) is not int or item["Size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", item.get("Sha256", ""))
                or not re.fullmatch(r"[0-9a-f]{40}", item.get("Revision", ""))):
            raise ValueError("检索模型文件清单无效")
    tracker = DownloadProgress(sum(item["Size"] for item in files.values()),
                               lambda fraction, message: progress(.1 + .8 * fraction, message, "preparing")
                               if progress else None)
    for name, item in files.items():
        _checkpoint(should_stop)
        tracker.source = "ModelScope"
        tracker.file("embedding", MODEL_ID, name, item["Size"])
        target = destination / name
        partial, record = target.with_name(target.name + ".part"), target.with_name(target.name + ".part.json")
        for path in (target, partial, record):
            if path.resolve() != path.absolute() or path.parent.resolve() != destination.resolve():
                raise ValueError("检索模型缓存不能经过链接或离开模型目录")
        if not _verified(target, item, tracker):
            expected = {"sha256": item["Sha256"], "size": item["Size"]}
            if read_json(record, {}) != expected:
                partial.unlink(missing_ok=True)
            atomic_json(record, expected)
            _download_weight(name, item, partial, tracker, should_stop)
            partial.replace(target)
            record.unlink(missing_ok=True)
        tracker.report(item["Size"])
        tracker.completed += item["Size"]
    fingerprint = hashlib.sha256(json.dumps(
        {name: item["Sha256"] for name, item in files.items()}, sort_keys=True).encode()).hexdigest()
    atomic_json(destination / "ready.json", {
        "fingerprint": fingerprint, "files": {name: item["Size"] for name, item in files.items()}})


def _validate_runtime(device, should_stop):
    verifier = EmbeddingWorker()
    try:
        verifier.encode(["课程检索模型准备验证"], should_stop=should_stop, _status={
            "ready": True, "device": device, "model_version": _weights_status()[1],
        })
    finally:
        verifier.close()


def prepare_model(device=None, progress=None, should_stop=None):
    from filelock import FileLock
    from classcue.preparation_progress import stage
    device = selected_device() if device is None else device
    if device not in {"cpu", "cuda"}:
        raise ValueError("检索设备必须为 cpu 或 cuda")
    if device == "cuda" and not _gpu_detection():
        raise RuntimeError("未检测到可用的 NVIDIA 显卡及驱动，请选择 CPU")
    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "semantic-search-setup.lock"), timeout=0):
        _checkpoint(should_stop)
        close_worker()
        directory = runtime_directory(device)
        if directory.resolve() != directory.absolute():
            raise ValueError("检索运行环境不能经过链接")
        reused = _runtime_ready(directory, device, legacy=directory.name == "semantic-search")
        if not reused:
            _prepare_environment(device, directory, progress, should_stop)
        _checkpoint(should_stop)
        _prepare_weights(progress, should_stop)
        if progress:
            progress(.95, "正在验证检索模型", "preparing")
        stage("verifying", "正在加载并验证检索模型", label="验证检索模型")
        _validate_runtime(device, should_stop)
        _checkpoint(should_stop)
        if not reused:
            atomic_json(directory / "ready.json", {
                "version": RUNTIME_VERSION, "device": device, "torch_variant": device})
        atomic_json(_selection_path(), {"device": device})
        return {"model_id": MODEL_ID, "device": device, "reused": reused}


class EmbeddingWorker:
    def __init__(self):
        self.lock = RLock()
        self.process = None
        self.job = None
        self.timer = None
        self.messages = Queue()
        self.identity = None
        self.query_cache = OrderedDict()

    def _start(self, status):
        from classcue.qwen_process import WindowsJob
        python = runtime_python(status["device"])
        identity = (status["device"], status["model_version"], str(python))
        if self.process and self.process.poll() is None and self.identity == identity:
            return
        self.close()
        self.messages = messages = Queue(maxsize=8)
        log_path = log_directory(PROJECT_ROOT) / "semantic-worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            self.process = process = subprocess.Popen(
                [str(python), "-m", "classcue.embedding_worker", "--model", str(model_directory()),
                 "--device", status["device"]], cwd=PROJECT_ROOT, env=_environment(status["device"]),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
                encoding="utf-8", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 4)
        try:
            self.job = WindowsJob(process)
            self.job.resume(process)
        except BaseException:
            self.close()
            raise
        self.identity = identity

        def read():
            try:
                for line in process.stdout:
                    if len(line) > 2 * 1024 * 1024:
                        break
                    messages.put(json.loads(line), timeout=1)
            except Exception:
                pass
            finally:
                try:
                    messages.put({"event": "exit"}, timeout=1)
                except Exception:
                    pass
        Thread(target=read, name="semantic-worker-events", daemon=True).start()

    def encode(self, texts, *, query=False, should_stop=None, _status=None):
        if not 1 <= len(texts) <= 16:
            raise ValueError("检索嵌入每批需要 1 至 16 个片段")
        with self.lock:
            if self.timer:
                self.timer.cancel()
            try:
                status = _status if _status is not None else model_status()
                if not status["ready"]:
                    raise RuntimeError("请先在设置 → 课程检索中准备 Qwen 检索模型")
                cache_key = (status["model_version"], status["device"], tuple(texts))
                if query and cache_key in self.query_cache:
                    self.query_cache.move_to_end(cache_key)
                    return self.query_cache[cache_key].copy()
                self._start(status)
                self.process.stdin.write(json.dumps({"texts": texts, "query": query}, ensure_ascii=True) + "\n")
                self.process.stdin.flush()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if should_stop and should_stop():
                        raise ProcessingCancelled("已停止语义索引")
                    try:
                        event = self.messages.get(timeout=.1)
                    except Empty:
                        continue
                    if event["event"] == "ready":
                        continue
                    if event["event"] == "error":
                        raise RuntimeError("检索模型推理失败：" + event["message"])
                    if event["event"] == "result":
                        vectors = np.frombuffer(base64.b64decode(event["vectors"], validate=True), dtype="<f4")
                        if (event["dimensions"] != DIMENSIONS or event["count"] != len(texts)
                                or vectors.size != len(texts) * DIMENSIONS or not np.isfinite(vectors).all()):
                            raise ValueError("检索模型返回无效向量")
                        vectors = vectors.reshape(len(texts), DIMENSIONS).copy()
                        if query:
                            self.query_cache[cache_key] = vectors
                            if len(self.query_cache) > 64:
                                self.query_cache.popitem(last=False)
                        return vectors.copy()
                    raise RuntimeError("检索模型进程已退出，请检查设备及可用显存")
                raise RuntimeError("检索模型响应超时，请缩小范围或检查设备")
            except BaseException:
                self.close()
                raise
            finally:
                if self.process and self.process.poll() is None:
                    self.timer = Timer(60, self.close)
                    self.timer.daemon = True
                    self.timer.start()

    def close(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            process, self.process = self.process, None
            if process:
                try:
                    if self.job:
                        self.job.close()
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                finally:
                    self.job = None
                    for pipe in (process.stdin, process.stdout):
                        if pipe:
                            pipe.close()


encoder = EmbeddingWorker()


def close_worker():
    encoder.close()


def release_idle_worker(job_type):
    if job_type in {"semantic_prepare", "semantic_index", "semantic_setup"}:
        return
    with encoder.lock:
        encoder.close()


atexit.register(close_worker)
