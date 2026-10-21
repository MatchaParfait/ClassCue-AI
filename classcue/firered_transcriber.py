"""Official FireRedVAD + AED system adapter for the subtitle contract."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
import logging
import math
from pathlib import Path
import re
import time
import unicodedata
from types import SimpleNamespace
from typing import Any, Callable

from classcue.core import TranscriptSegment
from classcue.qwen_transcriber import aligned_segments, stream_decode_audio_to_wav


@dataclass(frozen=True)
class TimedToken:
    text: str
    start: float
    end: float


def _value(item: Any, *names: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _tokens(output: Any) -> list[TimedToken]:
    """Normalize FireRed's AED timestamp objects across released API variants."""
    raw = _value(output, "word_timestamps", "timestamps", "tokens", "words", default=[])
    if not raw:
        raw = _value(output, "timestamp", default=[])
    if isinstance(raw, dict):
        raw = raw.get("items", raw.get("words", []))
    result = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            text, start, end = item[0], item[1], item[2]
        else:
            text = _value(item, "text", "word", "token", default=None)
            start = _value(item, "start", "start_time", "begin", default=None)
            end = _value(item, "end", "end_time", "finish", default=None)
        if not isinstance(text, str) or start is None or end is None:
            raise ValueError("FireRedASR2 返回了无效的字词时间戳。")
        if not text.strip():
            # AED's SentencePiece merger emits an empty word for a standalone
            # word-boundary token. It contains no recognized text to align.
            if not (math.isfinite(float(start)) and math.isfinite(float(end))
                    and 0 <= float(start) <= float(end)):
                raise ValueError("FireRedASR2 返回了无效的字词时间戳。")
            continue
        result.append(TimedToken(text.strip(), float(start), float(end)))
    return result


def _text(output: Any) -> str:
    value = _value(output, "text", "transcription", "sentence", default="")
    return "" if value is None else str(value).strip()


def _segments_from_tokens(
    tokens: list[TimedToken],
    text: str,
    first_id: int = 1,
    *,
    duration_seconds: float,
    offset_seconds: float = 0,
) -> list[TranscriptSegment]:
    """Reuse validated text alignment so punctuation and spaces retain their positions."""
    if text.strip() and not tokens:
        raise ValueError("FireRedASR2 返回了文本但没有时间戳，不能生成可靠字幕。")
    items = [
        # FireRed can retain punctuation in tokens (including <unk>); the
        # shared aligner compares letters/numbers and restores original text.
        # Normalize the comparison only, retaining original text and timing.
        SimpleNamespace(text="".join(
            character for character in token.text
            if character == "'" or unicodedata.category(character)[0] in {"L", "N"}
        ) or token.text, start_time=token.start, end_time=token.end)
        for token in tokens
    ]
    try:
        return aligned_segments(text, items, offset_seconds, duration_seconds, first_id)
    except ValueError as exc:
        raise ValueError(f"FireRedASR2 字词时间对齐无效：{exc}") from exc


def load_firered_model(
    model_path: str, vad_path: str, batch_size: int = 1, *, use_gpu: bool = True,
):
    if batch_size < 1:
        raise ValueError("FireRed batch size must be positive")
    import torch
    from fireredasr2s.fireredasr2 import FireRedAsr2Config
    from fireredasr2s.fireredasr2 import asr
    from fireredasr2s.fireredasr2system import FireRedAsr2System, FireRedAsr2SystemConfig
    from fireredasr2s.fireredvad import FireRedVadConfig

    if use_gpu and not torch.cuda.is_available():
        raise RuntimeError("FireRed GPU requires available CUDA; CPU fallback is disabled")
    if not use_gpu:
        from classcue.qwen_memory import ensure_memory_headroom

        ensure_memory_headroom()
    config = FireRedAsr2Config(
        use_gpu=use_gpu,
        use_half=use_gpu,
        beam_size=3,
        nbest=1,
        decode_max_len=0,
        softmax_smoothing=1.25,
        aed_length_penalty=0.6,
        eos_penalty=1.0,
        return_timestamp=True,
    )
    with _memory_mapped_weights(torch), _aed_checkpoint_loader(torch, asr):
        system = FireRedAsr2System(FireRedAsr2SystemConfig(
            asr_type="aed", asr_model_dir=model_path, asr_config=config,
            vad_model_dir=vad_path, vad_config=FireRedVadConfig(use_gpu=use_gpu),
            asr_batch_size=batch_size, enable_vad=True, enable_lid=False, enable_punc=False,
        ))
    _enable_strict_timestamps(system.asr)
    return system


@contextmanager
def _memory_mapped_weights(torch):
    import zipfile

    original = torch.load

    def load(path, *args, **kwargs):
        # Keep the official loader, but avoid a second 4.7 GB checkpoint allocation.
        if isinstance(path, (str, Path)) and zipfile.is_zipfile(path):
            kwargs.setdefault("mmap", True)
        return original(path, *args, **kwargs)

    torch.load = load
    try:
        yield
    finally:
        torch.load = original


@contextmanager
def _aed_checkpoint_loader(torch, asr):
    original = asr.load_fireredasr_aed_model

    def load(path):
        package = torch.load(path, map_location="cpu", weights_only=False)
        module = torch.nn.Module
        register = module.register_parameter

        def empty_parameter(self, name, parameter):
            if parameter is not None:
                parameter = torch.nn.Parameter(parameter.to("meta"), requires_grad=parameter.requires_grad)
            register(self, name, parameter)

        # Leave buffers on CPU: official positional encodings use scalar .item().
        module.register_parameter = empty_parameter
        try:
            model = asr.FireRedAsrAed.from_args(package["args"])
        finally:
            module.register_parameter = register
        model.load_state_dict(package["model_state_dict"], strict=True, assign=True)
        # assign=True replaces Parameter objects; restore the official decoder's tied weights.
        model.decoder.tgt_word_prj.weight = model.decoder.tgt_word_emb.weight
        return model

    asr.load_fireredasr_aed_model = load
    try:
        yield
    finally:
        asr.load_fireredasr_aed_model = original


def _enable_strict_timestamps(model) -> None:
    # torchaudio forced_align cannot consume BF16, even when the AED supports it.
    model.model.ctc.register_forward_hook(lambda _module, _inputs, output: output.float())
    original = model._get_and_fix_timestamp

    def timestamp(hyp, hyp_ids, duration):
        if not hyp_ids:
            return []
        if hyp.get("timestamp") is None:
            raise ValueError("FireRedASR2 强制对齐失败，拒绝使用平均分配的虚假时间戳。")
        return original(hyp, hyp_ids, duration)

    model._get_and_fix_timestamp = timestamp


@contextmanager
def _native_error_capture(system, on_phase=None):
    """Recover exceptions swallowed by upstream feature, decoder and alignment code."""
    errors = []
    patches = []

    def wrap(call):
        def captured(*args, **kwargs):
            try:
                return call(*args, **kwargs)
            except Exception as error:
                errors.append(error)
                raise
        return captured

    def patch(owner, name):
        original = getattr(owner, name)
        patches.append((owner, name, original))
        setattr(owner, name, wrap(original))

    class Extractor:
        def __init__(self, original):
            self.original = original

        def __call__(self, *args, **kwargs):
            return wrap(self.original)(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.original, name)

    try:
        if hasattr(system.asr, "feat_extractor"):
            original = system.asr.feat_extractor
            patches.append((system.asr, "feat_extractor", original))
            system.asr.feat_extractor = Extractor(original)
        if hasattr(system.asr, "model"):
            patch(system.asr.model, "transcribe")
            import torchaudio
            original_align = torchaudio.functional.forced_align
            patches.append((torchaudio.functional, "forced_align", original_align))
            def align(*args, **kwargs):
                if on_phase:
                    on_phase("alignment")
                result = wrap(original_align)(*args, **kwargs)
                if on_phase:
                    on_phase("inference")
                return result
            torchaudio.functional.forced_align = align
        yield errors
    finally:
        for owner, name, original in reversed(patches):
            setattr(owner, name, original)


@contextmanager
def _temporary_audio_directory(parent):
    from tempfile import TemporaryDirectory

    temporary = TemporaryDirectory(prefix="firered-", dir=parent)
    try:
        yield temporary.name
    finally:
        # Windows may briefly lock the WAV; cleanup must not mask ASR results/errors.
        for attempt in range(4):
            try:
                temporary.cleanup()
                break
            except OSError as exc:
                if isinstance(exc, PermissionError) and attempt < 3:
                    time.sleep(0.2 * 2**attempt)
                    continue
                logging.getLogger(__name__).warning(
                    "Temporary audio cleanup deferred for %s: %s", temporary.name, exc,
                )
                break


def transcribe_firered(
    system,
    audio_path: str | Path,
    progress: Callable[[float, str], None] | None = None,
    temporary_directory: str | Path | None = None,
    *,
    timeline_path: str | Path | None = None,
    checkpoints=None,
    on_resource=None,
    on_phase=None,
    metrics=None,
    on_transcript=None,
) -> tuple[list[TranscriptSegment], int]:
    if progress:
        progress(0.04 if timeline_path else 0.02, "读取音频并保留原视频时间轴")
    original_detect = system.vad.detect
    original_transcribe = system.asr.transcribe
    total = completed = 0
    cached_completed = newly_completed = 0
    word_counts: list[int] = []
    spans = []
    streamed_count = 0
    metrics = metrics if metrics is not None else {}
    metrics.setdefault("batches", [])

    def phase(value):
        if on_phase:
            on_phase(value)

    def resource():
        if on_resource:
            on_resource({"completed_segments": cached_completed + newly_completed if checkpoints else completed,
                         "total_segments": total,
                         "current_batch_size": getattr(getattr(system, "config", None), "asr_batch_size", 1)})

    def detect(*args, **kwargs):
        nonlocal total, spans, cached_completed
        _check_cpu_headroom(system)
        phase("vad")
        if progress:
            progress(0.05, "FireRedVAD 检测语音并切段")
        cached = checkpoints.load_vad() if checkpoints else None
        started = time.perf_counter()
        if cached is not None:
            detected, probabilities = cached, None
            metrics["vad_cache_hit"] = True
        else:
            detected, probabilities = original_detect(*args, **kwargs)
            if checkpoints:
                phase("checkpoint")
                checkpoints.save_vad(detected)
        metrics["vad_seconds"] = time.perf_counter() - started
        spans = detected["timestamps"]
        total = len(detected["timestamps"])
        if checkpoints:
            cached_completed, _ = checkpoints.counts()
        resource()
        if progress:
            progress(0.15, f"FireRedVAD 检测完成，共 {total} 个语音片段")
        return detected, probabilities

    def transcribe(ids, waves):
        nonlocal completed, newly_completed, streamed_count
        _check_cpu_headroom(system)
        phase("inference")
        chunks = ([checkpoints.chunk(index, spans[index])
                   for index in range(completed, completed + len(waves))] if checkpoints else [])
        outputs = [checkpoints.load(chunk) for chunk in chunks] if checkpoints else [None] * len(waves)
        pending = [index for index, output in enumerate(outputs) if output is None]
        started = time.perf_counter()
        if pending:
            with _native_error_capture(system, on_phase) as errors:
                try:
                    fresh = original_transcribe([ids[index] for index in pending],
                                                [waves[index] for index in pending])
                except Exception:
                    if errors:
                        raise errors[0]
                    raise
                if errors:
                    raise errors[0]
            if len(fresh) != len(pending):
                raise ValueError("FireRedASR2 返回了非预期数量的转写结果。")
            for index, output in zip(pending, fresh):
                outputs[index] = output
        for index, (output, (sample_rate, samples)) in enumerate(zip(outputs, waves)):
            text = _text(output)
            # The official system discards silence sentinel outputs before merging.
            if text and not re.search(r"(<blank>)|(<sil>)", text):
                tokens = _tokens(output)
                _segments_from_tokens(tokens, text, duration_seconds=len(samples) / sample_rate)
                word_counts.append(len(tokens))
                # The native system merges this field directly. Normalize both
                # cached and fresh outputs so its words, checkpoints and early
                # transcript all have the same token count and real timing.
                outputs[index] = {**output, "timestamp": [
                    (token.text, token.start, token.end) for token in tokens
                ]}
        if checkpoints:
            phase("checkpoint")
            from classcue.firered_resilience import validate_output
            for index in pending:
                validate_output(outputs[index], len(waves[index][1]) / waves[index][0], ids[index])
            for index in pending:
                checkpoints.save(chunks[index], outputs[index])
        if pending:
            metrics["batches"].append({
                "batch_size": len(pending), "seconds": time.perf_counter() - started,
                "speech_seconds": sum(len(waves[index][1]) / waves[index][0] for index in pending),
                "segment_seconds": [len(waves[index][1]) / waves[index][0] for index in pending],
            })
        metrics["cached_segments"] = metrics.get("cached_segments", 0) + len(waves) - len(pending)
        if on_transcript is not None and outputs:
            added = []
            for index, output in enumerate(outputs):
                text = _text(output)
                if not text or re.search(r"(<blank>)|(<sil>)", text):
                    continue
                # Match the official system's millisecond truncation exactly.
                offset = int(spans[completed + index][0] * 1000)
                tokens = [
                    TimedToken(
                        token.text, int(token.start * 1000 + offset) / 1000,
                        int(token.end * 1000 + offset) / 1000,
                    ) for token in _tokens(output)
                ]
                added.extend(_segments_from_tokens(
                    tokens, text, streamed_count + len(added) + 1,
                    duration_seconds=duration_ms / 1000,
                ))
            streamed_count += len(added)
            on_transcript(added, int(spans[completed + len(outputs) - 1][1] * 1000))
        completed += len(outputs)
        newly_completed += len(pending)
        resource()
        if progress:
            saved = cached_completed + newly_completed if checkpoints else completed
            progress(0.15 + 0.8 * saved / max(1, total),
                     f"FireRedASR2-AED 转写 {saved}/{total} · "
                     f"BS{getattr(getattr(system, 'config', None), 'asr_batch_size', 1)}")
        return outputs

    audio_context = nullcontext(None) if timeline_path else _temporary_audio_directory(temporary_directory)
    with audio_context as temporary:
        if timeline_path:
            import wave
            wav_path = Path(timeline_path)
            with wave.open(str(wav_path), "rb") as source:
                duration_ms = round(source.getnframes() * 1000 / source.getframerate())
        else:
            wav_path = Path(temporary) / "timeline.wav"
            duration_ms = stream_decode_audio_to_wav(audio_path, wav_path, progress)
        duration = duration_ms / 1000
        # Observe the official pipeline; do not duplicate its segmentation or offsets.
        system.vad.detect = detect
        system.asr.transcribe = transcribe
        try:
            phase("reading")
            native = system.process(str(wav_path), uttid="video")
        finally:
            system.vad.detect = original_detect
            system.asr.transcribe = original_transcribe

    sentences, words = native["sentences"], native["words"]
    if len(sentences) != len(word_counts) or sum(word_counts) != len(words):
        raise ValueError("FireRedASR2 流水线返回了不完整的字词时间戳。")
    result: list[TranscriptSegment] = []
    cursor = 0
    for sentence, count in zip(sentences, word_counts):
        # Official words already use the original timeline, so no second offset is added.
        tokens = [TimedToken(word["text"], word["start_ms"] / 1000, word["end_ms"] / 1000)
                  for word in words[cursor:cursor + count]]
        result.extend(_segments_from_tokens(
            tokens, sentence["text"], len(result) + 1, duration_seconds=duration,
        ))
        cursor += count
    if checkpoints:
        from dataclasses import asdict
        from classcue.firered_resilience import save_document
        from classcue.qwen_resilience import validate_segments
        phase("checkpoint")
        validate_segments(result, duration_ms)
        save_document(checkpoints.directory / "result.json", checkpoints.job_id,
                      segments=[asdict(segment) for segment in result], duration_ms=duration_ms)
    if progress:
        progress(1.0, f"FireRedVAD + AED 转写完成，共 {len(result)} 段")
    return result, duration_ms


def _check_cpu_headroom(system):
    if getattr(getattr(system.asr, "config", None), "use_gpu", None) is False:
        from classcue.qwen_memory import ensure_memory_headroom

        ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
