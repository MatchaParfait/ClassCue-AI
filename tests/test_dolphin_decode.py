import sys
from types import SimpleNamespace

import numpy as np
import pytest

from dolphin_decode import _TopKBatch, decoding_context, default_decoder_mode, host_ctc_search


def test_gpu_defaults_to_host_search_but_cpu_keeps_official_search():
    assert default_decoder_mode("gpu") == "host-search"
    assert default_decoder_mode("cpu") == "official"


def test_cached_topk_preserves_selection_order_and_exact_scores():
    class Values(np.ndarray):
        def topk(self, _size):
            raise AssertionError("CPU must not rerun top-k")

    values = np.array([[[-1.0, -1.0, -2.0]]]).view(Values)
    indices = np.array([[[9, 2, 7]]])
    frame = _TopKBatch(values, indices, (1, 1, 10))[0][0]
    scores, tokens = frame.topk(3)
    assert tokens.tolist() == [9, 2, 7]
    assert scores.tolist() == [-1, -1, -2]
    assert frame[7] == -2
    with pytest.raises(ValueError, match="beam size"):
        frame.topk(2)


def test_cpu_search_is_unchanged():
    probabilities = SimpleNamespace(device=SimpleNamespace(type="cpu"))
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return "unchanged"

    assert host_ctc_search(original, probabilities, "lengths", 10, blank_id=3) == "unchanged"
    assert calls == [((probabilities, "lengths", 10), {"blank_id": 3})]


def test_cuda_search_bulk_transfers_selected_scores_without_cpu_reselection():
    transferred = []

    class Transfer:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def cpu(self):
            transferred.append(self.name)
            return self.value

    class Probabilities:
        device = SimpleNamespace(type="cuda")
        shape = (2, 3, 7)

        def topk(self, count, dim):
            assert count == 2 and dim == -1
            return Transfer("scores", np.array([[[-1., -2.]] * 3] * 2)), Transfer(
                "indices", np.array([[[5, 2]] * 3] * 2))

    def official(probabilities, lengths, beam, *, blank_id):
        assert probabilities.shape == (2, 3, 7)
        assert lengths == [3, 2] and beam == 2 and blank_id == 1
        frame = probabilities[1][1]
        assert frame.topk(2)[1].tolist() == [5, 2]
        assert frame[2].item() == -2.
        return "official algorithm"

    assert host_ctc_search(official, Probabilities(), Transfer("lengths", [3, 2]),
                           2, blank_id=1) == "official algorithm"
    assert transferred == ["scores", "indices", "lengths"]


def test_rescoring_scores_bulk_transfer_and_successful_restoration(monkeypatch):
    calls = []

    class Tensor:
        def cpu(self):
            calls.append("cpu")
            return self

    a, b = Tensor(), Tensor()
    module = SimpleNamespace(ctc_prefix_beam_search=lambda *args: args,
                             attention_rescoring=lambda *args: args)
    original = module.ctc_prefix_beam_search
    monkeypatch.setitem(sys.modules, "dolphin", SimpleNamespace(model=module))
    monkeypatch.setitem(sys.modules, "dolphin.model", module)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())

    class Recognizer:
        def forward_attention_decoder(self, *args):
            calls.append(args)
            return a, b

    model = Recognizer()
    with decoding_context(model, mode="host-search"):
        assert model.forward_attention_decoder("inputs") == (a, b)
    assert calls == [("inputs",), "cpu", "cpu"]
    assert vars(model) == {}
    assert module.ctc_prefix_beam_search is original


def test_context_restores_global_and_instance_bindings_after_failure(monkeypatch):
    def function(*args, **kwargs):
        return args

    module = SimpleNamespace(ctc_prefix_beam_search=function, attention_rescoring=function)
    monkeypatch.setitem(sys.modules, "dolphin", SimpleNamespace(model=module))
    monkeypatch.setitem(sys.modules, "dolphin.model", module)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())

    class Recognizer:
        device = SimpleNamespace(type="cpu")
        forward_attention_decoder = staticmethod(function)
        _forward_encoder = staticmethod(function)
        ctc_logprobs = staticmethod(function)
        get_token_timestamp_torchaudio = staticmethod(function)

    model = Recognizer()
    with pytest.raises(RuntimeError, match="injected"):
        with decoding_context(model, mode="host-search", profile={}):
            assert module.ctc_prefix_beam_search is not function
            assert "forward_attention_decoder" in vars(model)
            raise RuntimeError("injected")
    assert module.ctc_prefix_beam_search is function
    assert module.attention_rescoring is function
    assert vars(model) == {}


def test_official_without_profile_does_not_import_or_patch():
    with decoding_context(None, mode="official"):
        pass
    with pytest.raises(ValueError, match="Unknown"):
        with decoding_context(None, mode="other"):
            pass
