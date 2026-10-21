"""Local-only loading based on DataoceanAI/Dolphin's pinned official loader.

Adapted from dolphin/transcribe.py at 78ea615194777f91a3970ff010b5d678d0bbf5f0
(Apache-2.0). The caller stream-validates the checkpoint; this loader omits
the official loader's duplicate whole-file checksum and download branches.
Model construction, checkpoint conversion and restoration are unchanged.
"""

from pathlib import Path


def load_verified_dolphin_checkpoint(directory, *, device):
    import torch
    import yaml
    from dolphin.model import init_speech_model
    from dolphin.transcribe import convert_v1_state_dict

    directory = Path(directory)
    with (directory / "train.yaml").open() as source:
        configs = yaml.load(source, Loader=yaml.Loader)
    configs["cmvn_conf"]["cmvn_file"] = str(
        directory / ("feats_stats.npz" if configs["cmvn"] == "global_mvn" else "global_cmvn")
    )
    configs["tokenizer_conf"]["symbol_table_path"] = str(directory / "units.txt")
    if "bpe_path" in configs["tokenizer_conf"]:
        configs["tokenizer_conf"]["bpe_path"] = str(directory / "bpe.model")

    model = init_speech_model(configs)
    state = torch.load(directory / "small.cn.pt", map_location="cpu")
    if "normalize.mean" in state:
        state = convert_v1_state_dict(state)

    context_state, recognizer_state = {}, {}
    for name, tensor in state.items():
        if name.startswith("context_module."):
            context_state[name.removeprefix("context_module.")] = tensor
        else:
            recognizer_state[name] = tensor
    model.load_state_dict(recognizer_state)

    if context_state and configs.get("context_module") == "cppn":
        from dolphin.hotword import HotwordEncoder

        context_config = configs.get("context_module_conf", {})
        encoder_size = configs.get("encoder_conf", {}).get("output_size", 768)
        encoder = HotwordEncoder(
            vocab_size=model.vocab_size,
            embedding_size=context_config.get("embedding_size", encoder_size),
            encoder_layers=context_config.get("encoder_layers", 2),
            attention_heads=context_config.get("attention_heads", 4),
            dropout_rate=context_config.get("dropout_rate", 0.0),
        )
        keys = set(encoder.state_dict())
        encoder.load_state_dict(
            {name: tensor for name, tensor in context_state.items() if name in keys},
            strict=False,
        )
        model.context_module = encoder

    model.model_configs = configs
    model = model.to(device)
    model.device = device
    model.eval()
    if "layer_norm_eps" in configs:
        for module in model.modules():
            if isinstance(module, torch.nn.LayerNorm):
                module.eps = float(configs["layer_norm_eps"])
    return model
