from types import SimpleNamespace
import sys

import pytest

from classcue.qwen_loading import model_loading


def test_standard_loading_never_patches_runtime():
    with model_loading() as settings:
        assert settings["effective_profile"] == "standard"
        assert not settings["allocator_warmup_skipped"]


def test_low_memory_patch_is_scoped_and_restored_after_failure(monkeypatch):
    calls = []

    def warmup(model, expanded_device_map, hf_quantizer):
        calls.append(True)

    module = SimpleNamespace(caching_allocator_warmup=warmup)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(modeling_utils=module))
    with pytest.raises(ValueError):
        with model_loading("low-memory") as settings:
            module.caching_allocator_warmup(None, None, None)
            assert settings["allocator_warmup_skipped"]
            assert settings["effective_profile"] == "low-memory"
            raise ValueError("injected")
    assert module.caching_allocator_warmup is warmup
    assert not calls


def test_unknown_transformers_interface_warns_and_keeps_standard_loading(monkeypatch):
    original = lambda other: None
    module = SimpleNamespace(caching_allocator_warmup=original)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(modeling_utils=module))
    with pytest.warns(RuntimeWarning, match="Unsupported"):
        with model_loading("low-memory") as settings:
            assert settings["effective_profile"] == "standard"
            assert settings["fallback_reason"]
    assert module.caching_allocator_warmup is original


def test_loading_profile_is_validated():
    with pytest.raises(ValueError):
        with model_loading("unknown"):
            pass
