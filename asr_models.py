"""Local ASR catalogue and application-owned, explicitly prepared model cache."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import shutil
from threading import RLock
from tempfile import NamedTemporaryFile
from typing import Callable

from asr_storage import active_storage_root as storage_root, application_config_directory


ROOT = Path(__file__).resolve().parent
MODEL_ROOT = storage_root(ROOT) / "models" / "asr"
SETTINGS_PATH = application_config_directory(ROOT) / "asr_settings.json"
DEFAULT_MODEL_ID = "whisper-turbo"
OPERATION_LOCK = RLock()
SETTINGS_LOCK = RLock()
Progress = Callable[[float, str], None]
FIRERED_CPU_NOTICE = (
    "FireRed CPU 处理已禁用：推理过慢且内存需求较高。"
    "请明确选择 GPU 并保存，或改用支持 CPU 的 Paraformer / Dolphin。"
)
NANO_RETIRED_NOTICE = (
    "Fun-ASR-Nano 已下架：当前时间戳路径无法执行真正批量识别，"
    "不再用于正式转写。请明确选择其他 ASR 模型；已有字幕和实验记录保留。"
)


@dataclass(frozen=True)
class ASRModel:
    id: str
    label: str
    engine: str
    repositories: tuple[tuple[str, str], ...]


MODELS = {
    model.id: model
    for model in (
        ASRModel("whisper-turbo", "Whisper turbo", "whisper",
                 (("recognizer", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),)),
        ASRModel("whisper-large-v3", "Whisper large-v3", "whisper",
                 (("recognizer", "Systran/faster-whisper-large-v3"),)),
        ASRModel("qwen3-asr-1.7b", "Qwen3-ASR 1.7B", "qwen",
                 (("recognizer", "Qwen/Qwen3-ASR-1.7B"),
                  ("aligner", "Qwen/Qwen3-ForcedAligner-0.6B"))),
        ASRModel("fireredasr2-aed", "FireRedASR2S（VAD + AED）", "firered",
                 (("recognizer", "FireRedTeam/FireRedASR2-AED"),
                  ("vad", "FireRedTeam/FireRedVAD"))),
        ASRModel("paraformer-large-cpu", "Paraformer-large（BiCIF）", "paraformer",
                 (("recognizer", "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
                  ("vad", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"))),
        ASRModel("dolphin-small-cn", "Dolphin 中文 small（CTC）", "dolphin",
                 (("recognizer", "DataoceanAI/dolphin-cn-dialect-small"),
                  ("vad", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"))),
    )
}
# Historical definitions remain readable for saved selections and cache management.
RETIRED_MODELS = {
    "fun-asr-nano-2512": ASRModel(
        "fun-asr-nano-2512", "Fun-ASR-Nano 2512", "nano",
        (("recognizer", "FunAudioLLM/Fun-ASR-Nano-2512"),
         ("vad", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")),
    ),
}
ALIASES = {
    "turbo": "whisper-turbo",
    "large-v3": "whisper-large-v3",
    "firered": "fireredasr2-aed",
    "firered-asr2-aed": "fireredasr2-aed",
    "fireredasr2s-aed": "fireredasr2-aed",
}
FIRERED_REQUIRED_FILES = {
    "recognizer": ("model.pth.tar", "cmvn.ark", "dict.txt", "train_bpe1000.model", "config.yaml"),
    "vad": ("VAD/model.pth.tar", "VAD/cmvn.ark"),
}
PARAFORMER_REQUIRED_FILES = {
    "recognizer": ("model.pt", "config.yaml", "configuration.json", "am.mvn",
                   "tokens.json", "seg_dict"),
    "vad": ("model.pt", "config.yaml", "configuration.json", "am.mvn"),
}
NANO_REQUIRED_FILES = {
    "recognizer": ("model.pt", "config.yaml", "configuration.json", "multilingual.tiktoken",
                   "Qwen3-0.6B/config.json", "Qwen3-0.6B/generation_config.json",
                   "Qwen3-0.6B/tokenizer.json", "Qwen3-0.6B/tokenizer_config.json",
                   "Qwen3-0.6B/vocab.json", "Qwen3-0.6B/merges.txt"),
    "vad": PARAFORMER_REQUIRED_FILES["vad"],
}
DOLPHIN_REQUIRED_FILES = {
    "recognizer": ("small.cn.pt", "train.yaml", "units.txt", "global_cmvn"),
    "vad": PARAFORMER_REQUIRED_FILES["vad"],
}
HUB_ALLOW_PATTERNS = (
    "*.json", "*.bin", "*.safetensors", "*.txt", "*.model", "*.tiktoken",
    "*.pt", "*.pth", "*.ckpt", "*.tar", "*.ark", "*.yaml", "*.yml",
    "vocabulary.*", "merges.*",
)


class ModelBusyError(RuntimeError):
    pass


def get_model(model_id: str) -> ASRModel:
    canonical = ALIASES.get(model_id, model_id)
    try:
        return MODELS[canonical] if canonical in MODELS else RETIRED_MODELS[canonical]
    except KeyError as exc:
        raise ValueError(f"Unknown ASR model: {model_id}") from exc


def require_selectable_model(model_id: str) -> ASRModel:
    model = get_model(model_id)
    if model.id in RETIRED_MODELS:
        raise ValueError(NANO_RETIRED_NOTICE)
    return model


def selected_model_id() -> str:
    with SETTINGS_LOCK:
        try:
            value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))["model_id"]
            model = get_model(value)
            return model.id if model.id in MODELS else DEFAULT_MODEL_ID
        except (OSError, ValueError, KeyError, TypeError):
            return DEFAULT_MODEL_ID


def selected_firered_device() -> str:
    with SETTINGS_LOCK:
        try:
            device = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("firered_device")
            return device if device in ("gpu", "cpu") else "gpu"
        except (OSError, ValueError, AttributeError):
            return "gpu"


def require_firered_gpu(device: str | None = None) -> None:
    if (device or selected_firered_device()) != "gpu":
        raise ValueError(FIRERED_CPU_NOTICE)


def selected_paraformer_device() -> str:
    with SETTINGS_LOCK:
        try:
            device = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("paraformer_device")
            return device if device in ("gpu", "cpu") else "cpu"
        except (OSError, ValueError, AttributeError):
            return "cpu"


def selected_nano_device() -> str:
    with SETTINGS_LOCK:
        try:
            device = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("nano_device")
            return device if device in ("gpu", "cpu") else "cpu"
        except (OSError, ValueError, AttributeError):
            return "cpu"


def selected_dolphin_device() -> str:
    with SETTINGS_LOCK:
        try:
            device = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("dolphin_device")
            return device if device in ("gpu", "cpu") else "cpu"
        except (OSError, ValueError, AttributeError):
            return "cpu"


def select_model(model_id: str, *, firered_device: str | None = None,
                 paraformer_device: str | None = None, nano_device: str | None = None,
                 dolphin_device: str | None = None) -> None:
    from filelock import FileLock

    model = require_selectable_model(model_id)
    if firered_device is not None and firered_device not in ("gpu", "cpu"):
        raise ValueError("FireRed device must be gpu or cpu")
    if firered_device == "cpu":
        raise ValueError(FIRERED_CPU_NOTICE)
    if model.engine == "firered":
        require_firered_gpu(firered_device)
    if paraformer_device is not None and paraformer_device not in ("gpu", "cpu"):
        raise ValueError("Paraformer device must be cpu or gpu")
    if nano_device is not None and nano_device not in ("gpu", "cpu"):
        raise ValueError("Nano device must be cpu or gpu")
    if dolphin_device is not None and dolphin_device not in ("gpu", "cpu"):
        raise ValueError("Dolphin device must be cpu or gpu")
    if model.engine == "dolphin":
        if (dolphin_device or selected_dolphin_device()) == "gpu":
            from dolphin_runtime import require_cuda

            require_cuda()
    elif dolphin_device is not None:
        raise ValueError("dolphin_device is only valid for Dolphin")
    if model.engine == "nano":
        if (nano_device or selected_nano_device()) == "gpu":
            from nano_runtime import require_cuda

            require_cuda()
    elif nano_device is not None:
        raise ValueError("nano_device is only valid for Fun-ASR-Nano")
    if model.engine == "paraformer":
        if (paraformer_device or selected_paraformer_device()) == "gpu":
            from paraformer_runtime import require_cuda

            require_cuda()
    elif paraformer_device is not None:
        raise ValueError("paraformer_device is only valid for Paraformer")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_LOCK, FileLock(str(SETTINGS_PATH.with_suffix(".lock")), timeout=10):
        _write_selection(model.id, firered_device=firered_device,
                         paraformer_device=paraformer_device, nano_device=nano_device,
                         dolphin_device=dolphin_device)


def _write_selection(model_id: str, *, firered_device: str | None = None,
                     paraformer_device: str | None = None, nano_device: str | None = None,
                     dolphin_device: str | None = None) -> None:
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    settings["model_id"] = model_id
    if firered_device is not None:
        settings["firered_device"] = firered_device
    if paraformer_device is not None:
        settings["paraformer_device"] = paraformer_device
    if nano_device is not None:
        settings["nano_device"] = nano_device
    if dolphin_device is not None:
        settings["dolphin_device"] = dolphin_device
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent,
            prefix="asr-settings-", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(settings, output)
        temporary.replace(SETTINGS_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _managed_path(path: Path) -> Path:
    root = MODEL_ROOT.resolve()
    resolved = path.resolve()
    if root != MODEL_ROOT.absolute() or resolved != path.absolute():
        raise ValueError("ASR cache paths must not traverse links or junctions")
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("ASR cache path escapes the model directory")
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("ASR cache must not be a symbolic link or junction")
    return resolved


def model_directory(model_id: str) -> Path:
    return _managed_path(MODEL_ROOT / get_model(model_id).id)


def component_paths(model_id: str) -> dict[str, Path]:
    model = get_model(model_id)
    return {name: _managed_path(model_directory(model.id) / name)
            for name, _repo in model.repositories}


@lru_cache(maxsize=16)
def _file_lock(path: str):
    from filelock import FileLock

    return FileLock(path)


@contextmanager
def model_operation(model_id: str):
    """Prevent deleting a model while any UI/CLI process is using it."""
    from filelock import Timeout

    model = get_model(model_id)
    if not OPERATION_LOCK.acquire(blocking=False):
        raise ModelBusyError("本地 ASR 正在运行，请等待转写或下载完成。")
    try:
        lock_dir = MODEL_ROOT / ".locks"
        _managed_path(lock_dir)
        lock_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(str(lock_dir / f"{model.id}.lock")).acquire(timeout=0):
            yield
    except Timeout as exc:
        raise ModelBusyError("该模型正在被其他进程使用，请等待任务完成。") from exc
    finally:
        OPERATION_LOCK.release()


def cache_size(model_id: str) -> int:
    directory = model_directory(model_id)
    if not directory.exists():
        return 0
    total = 0
    for file in directory.rglob("*"):
        if file.is_file() and not file.is_symlink():
            total += file.stat().st_size
    return total


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def model_is_downloaded(model_id: str) -> bool:
    model = get_model(model_id)
    directory = model_directory(model.id)
    try:
        manifest = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        if manifest["repositories"] != dict(model.repositories):
            return False
        files = manifest["files"]
        if not isinstance(files, dict) or not files:
            return False
        if model.engine == "firered":
            required = {f"{component}/{name}"
                        for component, names in FIRERED_REQUIRED_FILES.items() for name in names}
            if not required.issubset(files):
                return False
            if any(files[name] <= 0 for name in required if not name.endswith("config.yaml")):
                return False
        elif model.engine in {"paraformer", "nano", "dolphin"}:
            required_files = (DOLPHIN_REQUIRED_FILES if model.engine == "dolphin"
                              else NANO_REQUIRED_FILES if model.engine == "nano" else PARAFORMER_REQUIRED_FILES)
            required = {f"{component}/{name}"
                        for component, names in required_files.items() for name in names}
            if not required.issubset(files) or any(files[name] <= 0 for name in required):
                return False
            if model.engine == "nano" and manifest.get("ctc_tensor_count") != 86:
                return False
        for name, size in files.items():
            file = _managed_path(directory / name)
            if not file.is_relative_to(directory):
                return False
            if not file.is_file() or file.stat().st_size != size:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _firered_component_ready(directory: Path, component: str, repository: str) -> bool:
    """Reuse a complete legacy AED manifest without downloading its weights again."""
    try:
        manifest = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        if manifest["repositories"].get(component) != repository:
            return False
        for name in FIRERED_REQUIRED_FILES[component]:
            key = f"{component}/{name}"
            size = manifest["files"][key]
            file = _managed_path(directory / key)
            if (size <= 0 and not name.endswith("config.yaml")) or file.stat().st_size != size:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def download_model(model_id: str, progress: Progress | None = None, *,
                   reuse_legacy: bool = True) -> None:
    """Only called on explicit preparation or actual transcription, never selection."""
    model = require_selectable_model(model_id)
    with model_operation(model.id):
        if model_is_downloaded(model.id):
            return
        if model.engine == "dolphin":
            from dolphin_runtime import prepare_weights

            prepare_weights(progress)
            return
        if model.engine == "nano":
            from nano_runtime import prepare_weights

            prepare_weights(progress)
            return
        if model.engine == "paraformer":
            from paraformer_runtime import prepare_weights

            prepare_weights(progress, reuse_legacy=reuse_legacy)
            return
        import httpx
        import subprocess
        from processing_control import ProcessingCancelled, ResourcePaused

        try:
            if progress:
                progress(0.0, "从 ModelScope 下载模型")
            if model.engine == "firered":
                from scripts.prepare_firered_modelscope import prepare_model

                prepare_model(progress)
            else:
                from scripts.prepare_asr_modelscope import prepare_model

                prepare_model(model.id, progress)
            return
        except (ProcessingCancelled, ResourcePaused):
            raise
        except (OSError, httpx.HTTPError, RuntimeError, subprocess.SubprocessError) as exc:
            modelscope_error = str(exc)
            if progress:
                progress(0.0, "ModelScope 下载失败，尝试 Hugging Face 备用源")
        directory = model_directory(model.id)
        directory.mkdir(parents=True, exist_ok=True)
        paths = component_paths(model.id)
        from scripts.prepare_asr_huggingface import download_components
        from huggingface_hub.errors import HfHubHTTPError

        try:
            download_components(model, paths, directory, progress)
        except (ProcessingCancelled, ResourcePaused):
            raise
        except (OSError, HfHubHTTPError, httpx.TransportError, RuntimeError) as exc:
            raise RuntimeError(
                f"ModelScope 与 Hugging Face 均未能完成下载。"
                f"ModelScope: {modelscope_error}; Hugging Face: {exc}"
            ) from exc
        for name, repository in model.repositories:
            if model.engine == "firered" and _firered_component_ready(directory, name, repository):
                if progress:
                    progress(.99, f"复用并检查已下载的 {repository}")
                continue
            if progress:
                progress(.99, f"检查 {repository} 权重与配置")
            destination = paths[name]
            destination.mkdir(parents=True, exist_ok=True)
            if model.engine == "firered":
                if any(not (destination / file).is_file() or
                       ((destination / file).stat().st_size <= 0 and not file.endswith("config.yaml"))
                       for file in FIRERED_REQUIRED_FILES[name]):
                    raise RuntimeError(f"{repository} 下载不完整，请重试。")
                continue
            weights = (
                list(destination.glob("*.bin")) + list(destination.glob("*.safetensors"))
                + list(destination.glob("*.pt")) + list(destination.glob("*.pth"))
                + list(destination.glob("*.ckpt")) + list(destination.glob("*.tar"))
            )
            if (model.engine != "firered" and not (destination / "config.json").is_file()) or not weights:
                raise RuntimeError(f"{repository} 下载不完整，请重试。")
        files = {}
        for component in paths.values():
            for file in component.rglob("*"):
                if file.is_file() and ".cache" not in file.relative_to(component).parts:
                    _managed_path(file)
                    files[file.relative_to(directory).as_posix()] = file.stat().st_size
        manifest = {"repositories": dict(model.repositories), "files": files}
        temporary = directory / "ready.tmp"
        temporary.write_text(json.dumps(manifest), encoding="utf-8")
        temporary.replace(directory / "ready.json")
        if progress:
            progress(1.0, f"{model.label} 已就绪")


def delete_model(model_id: str, unload: Callable[[str], None] | None = None) -> int:
    from cleanup_progress import cleanup_directories
    with model_operation(model_id):
        directory = model_directory(model_id)
        if unload:
            unload(get_model(model_id).id)
        return cleanup_directories(MODEL_ROOT, [(directory, "模型权重")],
                                   scope=f"{get_model(model_id).label} 权重缓存")
