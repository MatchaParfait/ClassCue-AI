"""Scoped adapters around the pinned official search, preserving CUDA top-k order."""

from contextlib import contextmanager
from functools import wraps
from threading import RLock
from time import perf_counter

_LOCK = RLock()
DECODER_MODES = ("official", "host-search")


def default_decoder_mode(device):
    return "host-search" if device == "gpu" else "official"


class _TopKFrame:
    def __init__(self, values, indices):
        self.values, self.indices = values, indices
        self.positions = {int(token): i for i, token in enumerate(indices.tolist())}

    def topk(self, count):
        if count != len(self.indices):
            raise ValueError("Official CTC beam size changed after top-k extraction")
        return self.values, self.indices

    def __getitem__(self, token):
        return self.values[self.positions[token]]


class _TopKSequence:
    def __init__(self, values, indices):
        self.values, self.indices = values, indices

    def __getitem__(self, index):
        return _TopKFrame(self.values[index], self.indices[index])


class _TopKBatch:
    def __init__(self, values, indices, shape):
        self.values, self.indices, self.shape = values, indices, shape

    def __getitem__(self, index):
        return _TopKSequence(self.values[index], self.indices[index])


def host_ctc_search(official, probabilities, lengths, beam_size, *args, **kwargs):
    if probabilities.device.type != "cuda":
        return official(probabilities, lengths, beam_size, *args, **kwargs)
    # Keep selection and tie ordering on CUDA; only compact, exact scores cross to CPU.
    values, indices = probabilities.topk(beam_size, dim=-1)
    host = _TopKBatch(values.cpu(), indices.cpu(), probabilities.shape)
    return official(host, lengths.cpu(), beam_size, *args, **kwargs)


@contextmanager
def decoding_context(recognizer, *, mode="official", profile=None):
    """The worker serializes decoding; restore all bindings even on OOM or cancellation."""
    if mode not in DECODER_MODES:
        raise ValueError(f"Unknown Dolphin decoder mode: {mode}")
    if mode == "official" and profile is None:
        with _LOCK:
            yield
        return
    import dolphin.model as model_module
    import torch

    def sync():
        if str(recognizer.device).startswith("cuda"):
            torch.cuda.synchronize(recognizer.device)

    def timed(name, function):
        if profile is None:
            return function

        @wraps(function)
        def call(*args, **kwargs):
            sync()
            begin = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                sync()
                profile[name] = profile.get(name, 0.0) + perf_counter() - begin
        return call

    with _LOCK:
        originals = []

        def bind(owner, name, function):
            had_instance = name in vars(owner)
            original = getattr(owner, name)
            originals.append((owner, name, original, had_instance))
            setattr(owner, name, function)
            return original

        try:
            search = model_module.ctc_prefix_beam_search
            if mode == "host-search":
                def prefix(*args, **kwargs):
                    return host_ctc_search(search, *args, **kwargs)

                forward = recognizer.forward_attention_decoder

                def scores(*args, **kwargs):
                    outputs = forward(*args, **kwargs)
                    return tuple(tensor.cpu() for tensor in outputs)

                bind(recognizer, "forward_attention_decoder", scores)
            else:
                prefix = search
            bind(model_module, "ctc_prefix_beam_search", timed("ctc_search", prefix))
            bind(model_module, "attention_rescoring", timed("rescoring", model_module.attention_rescoring))
            if profile is not None:
                bind(recognizer, "_forward_encoder", timed("encoder", recognizer._forward_encoder))
                bind(recognizer, "ctc_logprobs", timed("ctc_projection", recognizer.ctc_logprobs))
                bind(recognizer, "get_token_timestamp_torchaudio",
                     timed("alignment", recognizer.get_token_timestamp_torchaudio))
            yield
        finally:
            for owner, name, original, had_instance in reversed(originals):
                if had_instance:
                    setattr(owner, name, original)
                else:
                    delattr(owner, name)
