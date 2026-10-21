import copy
import sys
from types import SimpleNamespace

import pytest

from classcue.asr_checkpoint import reuse_funasr_checkpoint_tensors


class AutoModel:
    pass


class Weight:
    def __init__(self):
        self.copies = 0

    def __deepcopy__(self, memo):
        self.copies += 1
        return Weight()


def official_loader(checkpoint, callback=None, *, fail=False):
    source = copy.deepcopy(checkpoint)
    for key in ("state_dict", "model_state_dict", "model"):
        if key in source:
            source = source[key]
    if fail:
        raise RuntimeError("checkpoint load failed")
    if callback:
        callback(frozenset(source))
    return source


@pytest.fixture
def loader_module(monkeypatch):
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "load_pretrained_model", official_loader, raising=False)
    return module


@pytest.mark.parametrize("wrapper", [None, "state_dict", "model_state_dict", "model"])
def test_scoped_loader_reuses_tensors_and_preserves_wrappers_and_callbacks(loader_module, wrapper):
    weight = Weight()
    state = {"encoder.weight": weight}
    checkpoint = {wrapper: state} if wrapper else state
    callbacks = []
    with reuse_funasr_checkpoint_tensors(AutoModel):
        result = loader_module.load_pretrained_model(checkpoint, callbacks.append)
        assert result["encoder.weight"] is weight
        if not wrapper:
            assert result is not checkpoint
    assert weight.copies == 0
    assert callbacks == [frozenset(state)]
    assert state == {"encoder.weight": weight}
    assert loader_module.load_pretrained_model is official_loader


def test_scoped_loader_does_not_change_global_deepcopy(loader_module):
    original = copy.deepcopy
    with reuse_funasr_checkpoint_tensors(AutoModel):
        weight = Weight()
        assert copy.deepcopy(weight) is not weight
        assert weight.copies == 1
    assert copy.deepcopy is original
    weight = Weight()
    assert loader_module.load_pretrained_model({"weight": weight})["weight"] is not weight
    assert weight.copies == 1


def test_scoped_loader_restores_on_load_failure_and_nested_context(loader_module):
    with pytest.raises(RuntimeError, match="checkpoint load failed"):
        with reuse_funasr_checkpoint_tensors(AutoModel):
            optimized = loader_module.load_pretrained_model
            with reuse_funasr_checkpoint_tensors(AutoModel):
                assert loader_module.load_pretrained_model is optimized
            assert loader_module.load_pretrained_model is optimized
            optimized({}, fail=True)
    assert loader_module.load_pretrained_model is official_loader


def test_unknown_loader_is_left_untouched(monkeypatch, loader_module):
    unknown = SimpleNamespace()
    monkeypatch.setattr(loader_module, "load_pretrained_model", unknown)
    with reuse_funasr_checkpoint_tensors(AutoModel):
        assert loader_module.load_pretrained_model is unknown
