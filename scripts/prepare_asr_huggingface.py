"""Per-file Hugging Face fallback with the same byte telemetry as ModelScope."""

import fnmatch
import io
import time
from contextvars import copy_context
from threading import RLock

from preparation_progress import DownloadProgress, stage, checkpoint


def download_components(model, paths, directory, progress=None):
    from huggingface_hub import HfApi, hf_hub_download
    from tqdm.auto import tqdm
    from asr_models import HUB_ALLOW_PATTERNS, _managed_path, _firered_component_ready

    bundles = []
    for component, repository in model.repositories:
        if model.engine == "firered" and _firered_component_ready(directory, component, repository):
            continue
        stage("metadata", f"Hugging Face · 获取 {repository} 文件清单",
              component=component, repository=repository, source="Hugging Face")
        info = HfApi().model_info(repository, files_metadata=True, timeout=15)
        patterns = ["VAD/*"] if model.engine == "firered" and component == "vad" else HUB_ALLOW_PATTERNS
        files = []
        for item in info.siblings:
            name = item.rfilename
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                continue
            path = _managed_path(paths[component] / name)
            if not path.is_relative_to(paths[component]) or item.size is None or item.size < 0:
                raise ValueError("Invalid Hugging Face file metadata")
            files.append(item)
        if not files:
            raise RuntimeError(f"Hugging Face repository is empty: {repository}")
        bundles.append((component, repository, info.sha, files))
    tracker = DownloadProgress(sum(item.size for *_, files in bundles for item in files),
                               progress, source="Hugging Face")
    for component, repository, revision, files in bundles:
        paths[component].mkdir(parents=True, exist_ok=True)
        for item in files:
            tracker.file(component, repository, item.rfilename, item.size)
            context = copy_context()
            lock = RLock()

            def report(size):
                with lock:
                    context.run(tracker.report, size)

            class FileProgress(tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs.update(file=io.StringIO(), disable=False)
                    super().__init__(*args, **kwargs)
                    self.last_report = 0
                    report(int(self.n))

                def update(self, n=1):
                    result = super().update(n)
                    if time.monotonic() - self.last_report >= .5:
                        report(int(self.n))
                        self.last_report = time.monotonic()
                    return result

            try:
                hf_hub_download(repo_id=repository, filename=item.rfilename, revision=revision,
                                local_dir=str(paths[component]), cache_dir=str(directory / ".hub-cache"),
                                tqdm_class=FileProgress)
            except Exception:
                # Some native transfer backends wrap callback exceptions.
                # Preserve a requested pause rather than treating it as a
                # network failure and switching download sources.
                checkpoint()
                raise
            tracker.report(item.size)
            tracker.completed += item.size
    stage("verifying", "检查 Hugging Face 下载的模型文件与分片", source="Hugging Face")
