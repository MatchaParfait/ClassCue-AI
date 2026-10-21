from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from dolphin_checkpoint import load_verified_dolphin_checkpoint
import dolphin_transcriber


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    config = {
        "cmvn": "global_cmvn", "cmvn_conf": {}, "tokenizer_conf": {"bpe_path": "old"},
        "context_module": "cppn", "layer_norm_eps": .0001,
    }
    (tmp_path / "train.yaml").write_text(yaml.safe_dump(config))
    calls = []
    state = {"encoder.weight": object(), "context_module.encoder.weight": object(),
             "context_module.training_only": object()}

    class LayerNorm:
        eps = 1

    class Model:
        vocab_size = 10

        def __init__(self):
            self.norm = LayerNorm()

        def load_state_dict(self, source):
            calls.append(("asr", source))

        def to(self, device):
            calls.append(("device", device))
            return self

        def eval(self):
            calls.append(("eval",))

        def modules(self):
            return [self, self.norm]

    class HotwordEncoder:
        def __init__(self, **kwargs):
            calls.append(("hotword_config", kwargs))

        def state_dict(self):
            return {"encoder.weight": None}

        def load_state_dict(self, source, strict):
            calls.append(("hotword_state", source, strict))

    def init(configs):
        calls.append(("config", deepcopy(configs)))
        return Model()

    def load(path, **kwargs):
        calls.append(("load", path, kwargs))
        return dict(state)

    def convert(source):
        calls.append(("convert",))
        source["encoder.weight"] = source.pop("normalize.mean")
        return source

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        load=load, nn=SimpleNamespace(LayerNorm=LayerNorm)))
    monkeypatch.setitem(sys.modules, "dolphin.model", SimpleNamespace(init_speech_model=init))
    monkeypatch.setitem(sys.modules, "dolphin.transcribe", SimpleNamespace(convert_v1_state_dict=convert))
    monkeypatch.setitem(sys.modules, "dolphin.hotword", SimpleNamespace(HotwordEncoder=HotwordEncoder))
    return SimpleNamespace(path=tmp_path, calls=calls, state=state, config=config)


@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
def test_verified_loader_keeps_official_restore_and_hotword_behavior(checkpoint, device):
    model = load_verified_dolphin_checkpoint(checkpoint.path, device=device)
    calls = checkpoint.calls
    config = calls[0][1]
    assert config["cmvn_conf"]["cmvn_file"] == str(checkpoint.path / "global_cmvn")
    assert config["tokenizer_conf"]["symbol_table_path"] == str(checkpoint.path / "units.txt")
    assert config["tokenizer_conf"]["bpe_path"] == str(checkpoint.path / "bpe.model")
    assert ("load", checkpoint.path / "small.cn.pt", {"map_location": "cpu"}) in calls
    assert next(c[1] for c in calls if c[0] == "asr") == {
        "encoder.weight": checkpoint.state["encoder.weight"]}
    assert ("hotword_state", {"encoder.weight": checkpoint.state["context_module.encoder.weight"]},
            False) in calls
    assert model.device == device
    assert model.norm.eps == .0001
    assert model.model_configs == config
    assert calls[-2:] == [("device", device), ("eval",)]


def test_global_mvn_and_v1_checkpoint_conversion_are_preserved(checkpoint):
    checkpoint.config["cmvn"] = "global_mvn"
    (checkpoint.path / "train.yaml").write_text(yaml.safe_dump(checkpoint.config))
    weight = checkpoint.state.pop("encoder.weight")
    checkpoint.state["normalize.mean"] = weight
    load_verified_dolphin_checkpoint(checkpoint.path, device="cpu")
    assert checkpoint.calls[0][1]["cmvn_conf"]["cmvn_file"] == str(
        checkpoint.path / "feats_stats.npz")
    assert ("convert",) in checkpoint.calls
    assert next(c[1] for c in checkpoint.calls if c[0] == "asr")["encoder.weight"] is weight


def test_production_checksum_is_streamed_and_verified_before_any_model_load(tmp_path, monkeypatch):
    from asr_models import DOLPHIN_REQUIRED_FILES
    from scripts import prepare_dolphin
    import dolphin_checkpoint

    for name in DOLPHIN_REQUIRED_FILES["recognizer"]:
        (tmp_path / name).write_bytes(b"checkpoint")
    calls = []
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None))
    monkeypatch.setitem(sys.modules, "dolphin.tokenizer", SimpleNamespace(
        init_tokenizer=lambda _configs: object()))
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=lambda **_: None))
    monkeypatch.setattr(dolphin_checkpoint, "load_verified_dolphin_checkpoint",
                        lambda *a, **kw: calls.append("load"))
    original_digest = hashlib.file_digest
    monkeypatch.setattr(hashlib, "file_digest", lambda source, algorithm: (
        calls.append(("digest", source.name, algorithm)) or original_digest(source, algorithm)))
    monkeypatch.setattr(prepare_dolphin, "CHECKPOINT_SHA256", "not-the-checkpoint")
    with pytest.raises(ValueError, match="official pinned checkpoint"):
        dolphin_transcriber.load_dolphin_model(tmp_path, tmp_path)
    assert calls == [("digest", str(tmp_path / "small.cn.pt"), "sha256")]


def test_verified_loader_never_reads_checkpoint_as_bytes(checkpoint, monkeypatch):
    original = Path.open

    def config_only(path, *args, **kwargs):
        assert path.suffix != ".pt"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", config_only)
    load_verified_dolphin_checkpoint(checkpoint.path, device="cpu")


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_production_loader_stream_checks_once_then_loads_asr_and_cpu_vad(tmp_path, monkeypatch, device):
    from asr_models import DOLPHIN_REQUIRED_FILES
    from scripts import prepare_dolphin
    import dolphin_checkpoint

    for name in DOLPHIN_REQUIRED_FILES["recognizer"]:
        (tmp_path / name).write_bytes(b"checkpoint")
    monkeypatch.setattr(prepare_dolphin, "CHECKPOINT_SHA256",
                        hashlib.sha256(b"checkpoint").hexdigest())
    calls = []
    original_open = Path.open

    class StreamOnly:
        def __init__(self, source):
            self.source = source

        def read(self, *_args):
            pytest.fail("Checkpoint checksum must not read the complete file")

        def readinto(self, buffer):
            return self.source.readinto(buffer)

        def readable(self):
            return True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.source.close()

    def streamed_open(path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        if path.name == "small.cn.pt":
            calls.append("checksum")
            return StreamOnly(source)
        return source

    monkeypatch.setattr(Path, "open", streamed_open)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: device == "gpu"),
        set_num_threads=lambda _: None, set_num_interop_threads=lambda _: None))

    def component(placement):
        return SimpleNamespace(
            parameters=lambda: [SimpleNamespace(device=SimpleNamespace(type=placement))],
            eval=lambda: None, model_configs={},
            vad_opts=SimpleNamespace(max_single_segment_time=30000))

    def load(path, **options):
        calls.append(("asr", path, options))
        return component("cuda" if device == "gpu" else "cpu")

    def vad(**options):
        calls.append(("vad", options))
        return SimpleNamespace(model=component("cpu"))

    monkeypatch.setattr(dolphin_checkpoint, "load_verified_dolphin_checkpoint", load)
    monkeypatch.setattr(dolphin_transcriber, "_validate_component", lambda path, _kind: path)
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=vad))
    monkeypatch.setitem(sys.modules, "dolphin.tokenizer", SimpleNamespace(
        init_tokenizer=lambda _: None))
    result = dolphin_transcriber.load_dolphin_model(tmp_path, tmp_path, device=device)
    assert result.device == device
    assert calls[0] == "checksum"
    assert calls[1] == ("asr", tmp_path, {"device": "cuda:0" if device == "gpu" else "cpu"})
    assert calls[2][0] == "vad"
    assert calls[2][1]["device"] == "cpu"
