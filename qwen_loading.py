"""Scoped low-memory loading for isolated Qwen workers."""

from contextlib import contextmanager
import inspect
from threading import RLock
import warnings

_LOCK = RLock()


@contextmanager
def model_loading(profile="standard"):
    if profile not in ("standard", "low-memory"):
        raise ValueError("Unknown loading profile")
    settings = {"requested_profile": profile, "effective_profile": "standard",
                "allocator_warmup_skipped": False, "fallback_reason": None}
    if profile == "standard":
        yield settings
        return
    from transformers import modeling_utils

    with _LOCK:
        original = getattr(modeling_utils, "caching_allocator_warmup", None)
        try:
            parameters = set(inspect.signature(original).parameters) if callable(original) else set()
        except (TypeError, ValueError):
            parameters = set()
        if not {"model", "expanded_device_map", "hf_quantizer"}.issubset(parameters):
            settings["fallback_reason"] = "Unsupported allocator warmup interface"
            warnings.warn(settings["fallback_reason"] + "; using standard loading", RuntimeWarning)
            yield settings
            return

        def skip(*args, **kwargs):
            settings["allocator_warmup_skipped"] = True

        modeling_utils.caching_allocator_warmup = skip
        settings["effective_profile"] = "low-memory"
        try:
            yield settings
        finally:
            modeling_utils.caching_allocator_warmup = original
