"""Optional PPT CUDA environment and one owned, reusable extraction worker."""

import atexit
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import subprocess
import sys
import sysconfig
from threading import RLock, Thread, Timer
import time

from classcue.asr_storage import active_storage_root, log_directory
from classcue.paths import PROJECT_ROOT
from classcue.processing_control import ProcessingCancelled


RUNTIME_VERSION = "slides-torch-2.7.1-cu128-v1"
PREFIX = "CLASSCUE_SLIDES:"


class SlideCudaError(RuntimeError):
    pass


def runtime_directory():
    return active_storage_root(PROJECT_ROOT) / ".runtime" / "slides-gpu"


def runtime_python():
    return runtime_directory() / "Scripts" / "python.exe"


def acceleration_status():
    try:
        record = json.loads((runtime_directory() / "ready.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid runtime record")
        ready = record.get("version") == RUNTIME_VERSION and runtime_python().is_file()
    except (OSError, ValueError, TypeError):
        ready, record = False, {}
    return {"ready": ready, "gpu_name": record.get("gpu_name") if ready else None,
            "message": "CUDA 环境已准备" if ready else "CUDA 环境尚未准备"}


def _environment():
    env = os.environ.copy()
    cache = active_storage_root(PROJECT_ROOT) / ".runtime" / "cache"
    for name, leaf in (("CUDA_CACHE_PATH", "cuda"), ("TORCH_HOME", "torch"),
                       ("TORCH_EXTENSIONS_DIR", "torch-extensions")):
        env.setdefault(name, str(cache / leaf))
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    return env


def _command(*arguments):
    return [str(runtime_python()), "-m", "classcue.slide_worker",
            "--site-packages", sysconfig.get_path("purelib"), *arguments]


def install_runtime(progress=None):
    from filelock import FileLock
    from classcue.preparation_progress import run_logged

    directory = runtime_directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory.absolute():
        raise ValueError("PPT CUDA 环境不能位于目录联接中")
    with FileLock(str(directory.parent / "slides-gpu-setup.lock"), timeout=0):
        close_workers()
        env = _environment()
        env.setdefault("UV_CACHE_DIR", str(directory.parent / "cache" / "uv"))
        env.setdefault("UV_PYTHON_INSTALL_DIR", str(PROJECT_ROOT / ".runtime" / "python"))
        env["UV_HTTP_TIMEOUT"] = "300"
        bundled = PROJECT_ROOT / ".runtime" / "bin" / "uv.exe"
        uv = str(bundled) if bundled.is_file() else shutil.which("uv")
        if uv:
            create = [uv, "venv", "--python", sys.executable, str(directory)]
            pip = [uv, "pip", "install", "--python", str(runtime_python())]
        else:
            create = [sys.executable, "-m", "venv", str(directory)]
            pip = [str(runtime_python()), "-m", "pip", "install"]
        commands = [] if runtime_python().is_file() else [create]
        wheel = directory.parent / "wheels" / "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl"
        commands.append(pip + ([str(wheel)] if wheel.is_file() and sys.version_info[:2] == (3, 12)
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
            progress(1.0, "PPT CUDA 环境已准备", "completed")


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
