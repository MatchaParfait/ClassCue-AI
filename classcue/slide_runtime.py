"""Optional PPT CUDA environment and one owned, reusable extraction worker."""

import atexit
from contextlib import nullcontext
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import stat
import subprocess
import sys
import sysconfig
from threading import RLock, Thread, Timer
import time

from classcue.asr_storage import active_storage_root, application_config_directory, log_directory
from classcue.desktop_runtime import atomic_json, read_json
from classcue.paths import PROJECT_ROOT
from classcue.processing_control import ProcessingCancelled


RUNTIME_VERSION = "slides-torch-2.7.1-cu128-v1"
PREFIX = "CLASSCUE_SLIDES:"
_STATUS_LOCK = RLock()
_STATUS_CACHE = None
_SELECTION_LOCK = RLock()


class SlideCudaError(RuntimeError):
    pass


def runtime_directory():
    return active_storage_root(PROJECT_ROOT) / ".runtime" / "slides-gpu"


def runtime_python():
    return runtime_directory() / "Scripts" / "python.exe"


def cache_directory():
    return runtime_directory().parent / "slides-cache"


def cached_wheel():
    return runtime_directory().parent / "wheels" / "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl"


def download_resources():
    return [(cache_directory(), "PPT 下载与计算缓存"), (cached_wheel(), "GPU 共享安装包")]


def installer_download_resources(*, include_wheel=True):
    targets = [(cache_directory() / leaf, "PPT 安装下载缓存") for leaf in ("uv", "pip")]
    if include_wheel:
        targets.append((cached_wheel(), "GPU 安装包"))
    return targets


def installer_cleanup_pending():
    return _runtime_record()[0] and any(resource_size(path) for path, _ in installer_download_resources())


def clear_installer_downloads(*, include_wheel=True, installation_locked=False):
    from filelock import FileLock
    from classcue.cleanup_progress import cleanup_directories

    root = runtime_directory().parent.absolute()
    lock = root / "slides-gpu-setup.lock"
    if root.resolve() != root or lock.resolve() != lock:
        raise ValueError("PPT 安装清理目录不能经过链接或目录联接")
    with nullcontext() if installation_locked else FileLock(str(lock), timeout=0):
        # Recheck under the setup lock: a failed reinstall must keep its downloads.
        if not _runtime_record()[0]:
            return 0
        return cleanup_directories(root, installer_download_resources(include_wheel=include_wheel),
                                   scope="PPT 安装包")


def resource_size(directory):
    """Count only owned regular files, without traversing links or junctions."""
    directory = Path(directory).absolute()
    if directory.resolve() != directory:
        raise ValueError("PPT 资源目录不能经过链接或目录联接")
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    total, pending = 0, [directory]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except FileNotFoundError:
            continue
        with entries as iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def clear_resources(scope, project_root=None):
    from classcue.cleanup_progress import cleanup_directories

    if scope not in ("runtime", "downloads", "all"):
        raise ValueError("无效的 PPT 清理范围")
    if scope != "downloads" and selected_backend(project_root) == "cuda":
        raise ValueError("请先将 PPT 默认设备切换到 CPU 或自动，再清理 GPU 环境。")
    root = runtime_directory().parent
    targets = []
    if scope in ("runtime", "all"):
        targets.append((runtime_directory(), "PPT GPU 环境"))
    if scope in ("downloads", "all"):
        targets.extend(download_resources())
    close_workers()
    with _STATUS_LOCK:
        try:
            return cleanup_directories(root, targets, scope="PPT 资源",
                                       locks=[root / "slides-gpu-setup.lock"])
        finally:
            invalidate_status()


def selection_path(project_root=None):
    return application_config_directory(project_root or PROJECT_ROOT) / "slide_settings.json"


def selected_backend(project_root=None):
    value = read_json(selection_path(project_root), {})
    backend = value.get("backend") if isinstance(value, dict) else None
    return backend if backend in ("auto", "cpu", "cuda") else "auto"


def select_backend(backend, project_root=None):
    from filelock import FileLock

    if backend not in ("auto", "cpu", "cuda"):
        raise ValueError("无效的 PPT 处理设备")
    if backend == "cuda":
        require_cuda(refresh=True)
    path = selection_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SELECTION_LOCK, FileLock(str(path.with_suffix(".lock")), timeout=10):
        atomic_json(path, {"backend": backend})


def _runtime_record():
    try:
        record = json.loads((runtime_directory() / "ready.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid runtime record")
        ready = record.get("version") == RUNTIME_VERSION and runtime_python().is_file()
    except (OSError, ValueError, TypeError):
        ready, record = False, {}
    return ready, record


def _detect_gpu():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        names = result.stdout.strip().splitlines()
        return names[0] if result.returncode == 0 and names else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _probe_cuda():
    result = subprocess.run(
        [str(runtime_python()), "-c",
         "import json, torch; "
         "x = torch.ones(8, device='cuda:0', dtype=torch.float64); "
         "assert x.sum().item() == 8; torch.cuda.synchronize(); "
         "print(json.dumps({'gpu_name': torch.cuda.get_device_name(0)}))"],
        capture_output=True, text=True, timeout=30, env=_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError("CUDA check failed")
    return json.loads(result.stdout)["gpu_name"]


def invalidate_status():
    global _STATUS_CACHE
    with _STATUS_LOCK:
        _STATUS_CACHE = None


def acceleration_status(*, refresh=False):
    """Separate detected hardware, prepared files and actual CUDA usability."""
    global _STATUS_CACHE
    with _STATUS_LOCK:
        prepared, record = _runtime_record()
        signature = (str(runtime_directory()), prepared, record.get("gpu_name"))
        if not refresh and _STATUS_CACHE is not None:
            timestamp, previous, status = _STATUS_CACHE
            if previous == signature and time.monotonic() - timestamp < 30:
                return dict(status)
        name = _detect_gpu()
        status = {
            "ready": False, "runtime_ready": prepared,
            "gpu_detected": bool(name), "gpu_prepare_available": bool(name), "gpu_name": name,
            "gpu_unavailable_reason": "GPU 环境尚未准备，请先准备 GPU 环境。" if name else
                "未检测到可用的 NVIDIA 显卡及驱动，GPU 已禁用。",
        }
        if name and prepared:
            try:
                status.update(ready=True, gpu_name=_probe_cuda(), gpu_unavailable_reason=None)
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                status["gpu_unavailable_reason"] = "GPU 运算验证失败，请检查 NVIDIA 驱动或重新准备环境。"
        status["message"] = "GPU 环境已准备" if status["ready"] else status["gpu_unavailable_reason"]
        _STATUS_CACHE = (time.monotonic(), signature, status)
        return dict(status)


def require_cuda(*, refresh=False):
    status = acceleration_status(refresh=refresh)
    if not status["ready"]:
        raise SlideCudaError(status["gpu_unavailable_reason"])


def require_gpu_hardware():
    if not _detect_gpu():
        raise ValueError("未检测到可用的 NVIDIA 显卡及驱动，无法准备 GPU 环境。")


def _environment():
    env = os.environ.copy()
    cache = cache_directory()
    for name, leaf in (("CUDA_CACHE_PATH", "cuda"), ("TORCH_HOME", "torch"),
                       ("TORCH_EXTENSIONS_DIR", "torch-extensions"),
                       ("UV_CACHE_DIR", "uv"), ("PIP_CACHE_DIR", "pip")):
        env[name] = str(cache / leaf)
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    return env


def _command(*arguments):
    return [str(runtime_python()), "-m", "classcue.slide_worker",
            "--site-packages", sysconfig.get_path("purelib"), *arguments]


def install_runtime(progress=None):
    from filelock import FileLock
    from classcue.preparation_progress import run_logged

    require_gpu_hardware()
    invalidate_status()
    directory = runtime_directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if (directory.resolve() != directory.absolute()
            or cache_directory().resolve() != cache_directory().absolute()):
        raise ValueError("PPT CUDA 环境不能位于目录联接中")
    with FileLock(str(directory.parent / "slides-gpu-setup.lock"), timeout=0):
        close_workers()
        env = _environment()
        env.setdefault("UV_CACHE_DIR", str(directory.parent / "cache" / "uv"))
        env.setdefault("UV_PYTHON_INSTALL_DIR", str(PROJECT_ROOT / ".runtime" / "python"))
        env["UV_HTTP_TIMEOUT"] = "300"
        bundled = PROJECT_ROOT / ".runtime" / "bin" / "uv.exe"
        local = Path(sys.executable).with_name("uv.exe")
        uv = next((str(path) for path in (bundled, local) if path.is_file()), None)
        uv = uv or shutil.which("uv", path=env.get("PATH"))
        if uv:
            create = [uv, "venv", "--python", sys.executable, str(directory)]
            pip = [uv, "pip", "install", "--python", str(runtime_python())]
        else:
            create = [sys.executable, "-m", "venv", str(directory)]
            pip = [str(runtime_python()), "-m", "pip", "install"]
        commands = [] if runtime_python().is_file() else [create]
        wheel = cached_wheel()
        use_wheel = wheel.is_file() and sys.version_info[:2] == (3, 12)
        commands.append(pip + ([str(wheel)] if use_wheel
                               else ["torch==2.7.1", "--index-url", "https://download.pytorch.org/whl/cu128"]))
        log_path = log_directory(PROJECT_ROOT) / "slides-gpu-install.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        (directory / "ready.json").unlink(missing_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            for index, command in enumerate(commands):
                if progress:
                    progress(index / (len(commands) + 1), "正在准备 PPT CUDA 环境", "preparing")
                result = run_logged(
                    command, progress=progress, step=index + 1, steps=len(commands) + 1,
                    stdout=log, stderr=log, cwd=PROJECT_ROOT, env=env, timeout=3600,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode:
                    raise RuntimeError("PPT CUDA 环境准备失败，请检查网络后重试")
        if progress:
            progress(0.9, "正在验证 PPT CUDA 运算", "preparing")
        output = StringIO()
        probe = run_logged(_command("--probe"), cwd=PROJECT_ROOT, env=env, stdout=output,
                           progress=progress, step=len(commands) + 1, steps=len(commands) + 1,
                           timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        records = [line[len(PREFIX):] for line in output.getvalue().splitlines() if line.startswith(PREFIX)]
        value = json.loads(records[-1]) if records else {}
        if probe.returncode or value.get("event") != "ready":
            raise RuntimeError("PPT CUDA 验证失败，请确认 NVIDIA 驱动及可用显存")
        temporary = directory / "ready.tmp"
        temporary.write_text(json.dumps({"version": RUNTIME_VERSION, "gpu_name": value["gpu_name"]}),
                             encoding="utf-8")
        temporary.replace(directory / "ready.json")
        if progress:
            progress(0.97, "GPU 已通过验证，正在清理安装包", "preparing")
        message = "PPT GPU 环境已准备，安装包已自动清理"
        try:
            clear_installer_downloads(include_wheel=use_wheel, installation_locked=True)
        except (OSError, ValueError) as error:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\nInstaller cache cleanup failed: {error}\n")
            message = "PPT GPU 环境已准备，部分安装包未能清理，可在 PPT 设置中清理剩余缓存。"
        invalidate_status()
        if progress:
            progress(1.0, message, "completed")
        return message


class SlideWorker:
    def __init__(self):
        self.lock = RLock()
        self.process = None
        self.job = None
        self.messages = Queue()
        self.timer = None
        self.epoch = 0
        self.failed_until = 0

    def _start(self):
        from classcue.qwen_process import WindowsJob

        if self.process and self.process.poll() is None:
            return
        if self.process:
            self._close_locked()
        if not acceleration_status()["ready"]:
            raise SlideCudaError("请先准备 PPT CUDA 环境")
        self.messages = Queue(maxsize=64)
        messages = self.messages
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        log_path = log_directory(PROJECT_ROOT) / "slides-gpu-worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                _command(), cwd=PROJECT_ROOT, env=_environment(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", bufsize=1,
                creationflags=flags,
            )
        self.process = process
        try:
            self.job = WindowsJob(process)
            self.job.resume(process)
        except BaseException:
            self._close_locked()
            raise

        def read():
            try:
                for line in process.stdout:
                    if len(line) > 8 * 1024 * 1024:
                        break
                    if line.startswith(PREFIX):
                        value = json.loads(line[len(PREFIX):])
                        if not isinstance(value, dict):
                            break
                        messages.put(value, timeout=1)
            except (OSError, ValueError, Full):
                pass
            finally:
                try:
                    messages.put_nowait({"event": "exit"})
                except Full:
                    pass

        Thread(target=read, name="slide-worker-events", daemon=True).start()

    def _send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def extract(self, source, staging, source_hash, *, roi, ignore_regions, progress, should_stop):
        from classcue.slide_extractor import SlideDraft, SlideExtractionResult, SlideRoi

        with self.lock:
            self.epoch += 1
            epoch = self.epoch
            if self.timer:
                self.timer.cancel()
            try:
                if should_stop and should_stop():
                    raise ProcessingCancelled("已停止 PPT 提取")
                self._start()
                self._send({"action": "extract", "source": str(source), "staging": str(staging),
                            "source_hash": source_hash, "roi": asdict(roi),
                            "ignore_regions": [asdict(region) for region in ignore_regions]})
                last_message = time.monotonic()
                stopped = None
                while True:
                    now = time.monotonic()
                    if should_stop and should_stop() and stopped is None:
                        stopped = now
                        self._send({"action": "cancel"})
                    if stopped is not None and now - stopped > 5:
                        raise ProcessingCancelled("已停止 PPT 提取")
                    if now - last_message > 120:
                        raise SlideCudaError("PPT CUDA 进程长时间无响应")
                    try:
                        event = self.messages.get(timeout=0.1)
                    except Empty:
                        continue
                    last_message = time.monotonic()
                    kind = event.get("event")
                    if stopped is not None:
                        if kind in {"result", "error", "exit"}:
                            raise ProcessingCancelled("已停止 PPT 提取")
                        continue
                    if kind == "progress" and progress:
                        progress(event["value"], "CUDA · " + event["message"], event.get("stage"))
                    elif kind == "error":
                        if event.get("code") == "cancelled":
                            raise ProcessingCancelled("已停止 PPT 提取")
                        if event.get("code") == "source":
                            raise ValueError(event["message"])
                        raise SlideCudaError(event.get("message", "PPT CUDA 处理失败"))
                    elif kind == "exit":
                        raise SlideCudaError("PPT CUDA 进程已退出")
                    elif kind == "result":
                        try:
                            payload = event["result"]
                            payload["roi"] = SlideRoi(**payload["roi"])
                            payload["ignore_regions"] = tuple(SlideRoi(**r) for r in payload["ignore_regions"])
                            slides = []
                            for item in payload["slides"]:
                                item["image_path"] = Path(item["image_path"])
                                if not item["image_path"].resolve().is_relative_to(staging.resolve()):
                                    raise SlideCudaError("PPT CUDA 返回了无效的截图路径")
                                item["roi"] = SlideRoi(**item["roi"])
                                slides.append(SlideDraft(**item))
                            payload["slides"] = slides
                            return SlideExtractionResult(**payload)
                        except (KeyError, TypeError, ValueError) as error:
                            raise SlideCudaError("PPT CUDA 返回了无效的提取结果") from error
            except ProcessingCancelled:
                self._close_locked()
                raise
            except OSError as error:
                self._close_locked()
                if should_stop and should_stop():
                    raise ProcessingCancelled("已停止 PPT 提取") from error
                raise SlideCudaError("PPT CUDA 进程连接中断") from error
            except (KeyError, TypeError) as error:
                self._close_locked()
                raise SlideCudaError("PPT CUDA 进程返回了无效消息") from error
            except (ValueError, RuntimeError):
                self._close_locked()
                raise
            finally:
                if self.process and self.process.poll() is None:
                    self.timer = Timer(300, self._expire, args=(epoch,))
                    self.timer.daemon = True
                    self.timer.start()

    def _expire(self, epoch):
        with self.lock:
            if self.epoch == epoch:
                self._close_locked()

    def _close_locked(self):
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

    def close(self):
        with self.lock:
            self.epoch += 1
            self._close_locked()


_worker = SlideWorker()


def close_workers():
    _worker.close()


def release_idle_worker(job_type):
    if job_type in {"slide_extract", "slide_prepare", "course_sequence"}:
        return
    if _worker.lock.acquire(blocking=False):
        try:
            _worker.close()
        finally:
            _worker.lock.release()


atexit.register(close_workers)


def extract_with_backend(source, staging, source_hash, *, duration_ms, backend,
                         roi, ignore_regions, progress, should_stop):
    from classcue.slide_extractor import extract_slides

    if should_stop and should_stop():
        raise ProcessingCancelled("已停止 PPT 提取")
    if backend == "cuda":
        require_cuda()
    use_cuda = backend == "cuda" or (
        backend == "auto" and duration_ms >= 120_000 and acceleration_status()["ready"]
        and time.monotonic() >= _worker.failed_until)
    if use_cuda:
        if progress:
            progress(0, "正在使用 CUDA 提取 PPT", "preparing")
        try:
            return _worker.extract(source, staging, source_hash, roi=roi, ignore_regions=ignore_regions,
                                   progress=progress, should_stop=should_stop)
        except SlideCudaError:
            _worker.failed_until = time.monotonic() + 300
            if backend == "cuda":
                raise
            # A crashed worker cannot clean its scratch files. This generation
            # is unpublished and belongs only to the current extraction.
            directory = staging.resolve()
            if directory.exists():
                for path in directory.iterdir():
                    if path.resolve().parent != directory:
                        raise SlideCudaError("PPT 临时文件路径异常，请重新提取")
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            if progress:
                progress(0, "CUDA 暂不可用，改用 CPU 提取 PPT", "detecting")
    return extract_slides(source, staging, source_hash, roi=roi, ignore_regions=ignore_regions,
                          progress=progress, should_stop=should_stop, prefetch=True)
