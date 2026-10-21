"""Scoped checkpoint loading without redundant FunASR tensor copies."""

from contextlib import contextmanager
import copy
import inspect
from threading import RLock
from types import FunctionType, SimpleNamespace


_FUNASR_LOAD_LOCK = RLock()


@contextmanager
def reuse_funasr_checkpoint_tensors(auto_model):
    with _FUNASR_LOAD_LOCK:
        module = inspect.getmodule(auto_model)
        loader = getattr(module, "load_pretrained_model", None)
        if not isinstance(loader, FunctionType) or loader.__globals__.get("copy") is not copy:
            yield
            return

        # Keep official key mapping and callbacks, but copy only the dictionary.
        # Isolated globals avoid changing Python's copy module or other loaders.
        namespace = dict(loader.__globals__)
        namespace["copy"] = SimpleNamespace(copy=copy.copy, deepcopy=copy.copy)
        optimized = FunctionType(
            loader.__code__, namespace, loader.__name__, loader.__defaults__, loader.__closure__,
        )
        optimized.__kwdefaults__ = loader.__kwdefaults__
        module.load_pretrained_model = optimized
        try:
            yield
        finally:
            module.load_pretrained_model = loader
