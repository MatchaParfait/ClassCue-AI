"""Isolated, snapshot-approved classroom extraction experiments."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import time
from typing import Callable, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_provider import ProviderProfile
from core import (
    TranscriptSegment, build_semantic_chunks, filter_analysis_segments,
    find_all_trigger_matches, merge_candidate_windows,
)
from openai_judge import _response_format_unsupported
from processing_control import ProcessingCancelled
from video_store import VideoStore


PROMPT_DIRECTORY = Path(__file__).parent / "prompts" / "exam_lab"
PIPELINE_VERSION = "exam-lab-v1"


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheme: Literal["rules", "few_shot", "context_review"] = "rules"
    chunk_seconds: int = Field(default=300, ge=30, le=3600)
    overlap_seconds: int = Field(default=30, ge=0)
    review_context_seconds: int = Field(default=60, ge=0, le=600)
    max_tokens: int = Field(default=4096, ge=256, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    request_retries: int = Field(default=1, ge=0, le=5)
    repair_json: bool = True
    max_format_repairs: int = Field(default=1, ge=0, le=5)
    match_evidence_quotes: bool = True
    max_evidence_repairs: int = Field(default=2, ge=0, le=5)
    merge_min_chars: int = Field(default=20, ge=0, le=200)
    merge_max_gap_ms: int = Field(default=2000, ge=0, le=10000)
    merge_max_span_seconds: int = Field(default=30, ge=1, le=120)
    filter_formula_noise: bool = False
    use_keyword_prefilter: bool = False
    keywords: list[str] = Field(default_factory=list)
    filter_formula: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_options(self) -> "LabConfig":
        if self.overlap_seconds >= self.chunk_seconds:
            raise ValueError("overlap_seconds must be smaller than chunk_seconds")
        self.keywords = list(dict.fromkeys(word.strip() for word in self.keywords if word.strip()))
        if self.keywords:
            self.use_keyword_prefilter = True
        return self


class LabCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    level: Literal["confirmed"]
    signal_type: Literal["explicit_exam", "emphasis", "easy_error"]
    start_sentence_id: int
    end_sentence_id: int
    evidence_sentence_ids: list[int] = Field(min_length=1)
    title: str = Field(min_length=1)
    reason: str
    evidence_quote: str = Field(min_length=1)


class LabResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidates: list[LabCandidate]


def digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_prompt_bundle(
    directory: Path = PROMPT_DIRECTORY, *,
    variant: Literal["high_recall", "precision", "low_recall"] = "high_recall",
) -> dict:
    if variant not in ("high_recall", "precision", "low_recall"):
        raise ValueError(f"Unknown prompt variant: {variant}")
    bundle = {name: (directory / f"{name}.md").read_text(encoding="utf-8").strip()
              for name in ("system", "rules", "review", "repair", "task", "evidence_repair")}
    bundle["examples"] = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
    if variant != "high_recall":
        overlay = directory / variant
        for name in ("rules", "review", "task"):
            bundle[name] = (overlay / f"{name}.md").read_text(encoding="utf-8").strip()
        bundle["examples"] = json.loads((overlay / "examples.json").read_text(encoding="utf-8"))
    bundle["version"] = digest(bundle)
    return bundle


def _data_block(name: str, payload: object) -> str:
    # JSON escapes prevent transcript text from closing the surrounding delimiter.
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<{name}>\n{text}\n</{name}>"


def numbered_transcript(segments: list[TranscriptSegment]) -> str:
    lines = []
    for segment in segments:
        # Keep each segment on one line and prevent text from forging markers or delimiters.
        text = json.dumps(segment.text, ensure_ascii=False)[1:-1]
        for character, escaped in (
            ("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
            ("{", "\\u007b"), ("}", "\\u007d"),
            ("\u2028", "\\u2028"), ("\u2029", "\\u2029"),
        ):
            text = text.replace(character, escaped)
        lines.append(f"{{{segment.id}}} {text}")
    return "\n".join(lines)


def _transcript_block(segments: list[TranscriptSegment]) -> str:
    return f"<transcript_text>\n{numbered_transcript(segments)}\n</transcript_text>"


def render_examples(examples: list[dict]) -> str:
    sections = []
    for index, example in enumerate(examples, 1):
        segments = [TranscriptSegment(line["id"], 0, 0, line["text"])
                    for line in example["transcript"]]
        sections.append("\n".join([
            f"合成示例 {index}",
            _data_block("example_label_json", example["label"]),
            _transcript_block(segments),
            _data_block("expected_response_json", example["expected"]),
        ]))
    return "<synthetic_examples>\n" + "\n\n".join(sections) + "\n</synthetic_examples>"


def render_messages(
    bundle: dict, config: LabConfig, segments: list[TranscriptSegment],
    *, candidate: dict | None = None, repair_output: str | None = None,
    repair_errors: list[dict] | None = None,
) -> list[dict]:
    sections = [bundle["rules"]]
    if candidate is not None:
        sections += [bundle["review"], _data_block("candidate_json", candidate)]
    elif repair_output is not None:
        sections.append(_data_block("invalid_output_json", repair_output))
        sections.append(_data_block("format_errors_json", repair_errors or []))
    elif config.scheme == "few_shot" and bundle["examples"]:
        sections.append(render_examples(bundle["examples"]))
    sections.append(_transcript_block(segments))
    sections.append(bundle["repair"] if repair_output is not None else bundle["task"])
    return [
        {"role": "system", "content": bundle["system"]},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def request_body(profile: ProviderProfile, config: LabConfig, messages: list[dict]) -> dict:
    body = {"model": profile.model, "messages": messages,
            profile.token_parameter: config.max_tokens}
    if profile.json_mode:
        body["response_format"] = {"type": "json_object"}
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.reasoning_effort is not None:
        body["reasoning_effort"] = config.reasoning_effort
    return body


def _eligible_segments(segments: list[TranscriptSegment], config: LabConfig) -> list[TranscriptSegment]:
    excluded = [re.sub(r"\s+", "", value).casefold()
                for value in re.split(r"[,，;；\n]+", config.filter_formula or "") if value.strip()]
    eligible = [segment for segment in segments
                if not any(value in re.sub(r"\s+", "", segment.text).casefold() for value in excluded)]
    groups = {"custom": {"user": config.keywords}} if config.keywords else None
    if config.filter_formula_noise:
        eligible = filter_analysis_segments(eligible, groups)
    return eligible


def _build_input_view(
    full: list[TranscriptSegment], eligible: list[TranscriptSegment], config: LabConfig,
) -> tuple[list[TranscriptSegment], dict[int, list[int]]]:
    eligible_ids = {segment.id for segment in eligible}
    merged, sources, pending = [], {}, []
    pending_chars = 0

    def flush() -> None:
        if pending:
            first, last = pending[0], pending[-1]
            input_id = len(merged) + 1
            merged.append(TranscriptSegment(
                input_id, first.start_ms, last.end_ms, " ".join(segment.text for segment in pending),
            ))
            sources[input_id] = [segment.id for segment in pending]
            pending.clear()

    for segment in full:
        if config.merge_min_chars == 0 or segment.id not in eligible_ids:
            # Unsupplied fragments remain as boundaries for later range validation.
            flush()
            pending_chars = 0
            input_id = len(merged) + 1
            merged.append(TranscriptSegment(input_id, segment.start_ms, segment.end_ms, segment.text))
            sources[input_id] = [segment.id]
            continue
        if pending and (
            segment.start_ms - pending[-1].end_ms > config.merge_max_gap_ms
            or segment.end_ms - pending[0].start_ms > config.merge_max_span_seconds * 1000
        ):
            flush()
            pending_chars = 0
        pending.append(segment)
        pending_chars += len(re.sub(r"\s+", "", segment.text))
        if pending_chars >= config.merge_min_chars:
            flush()
            pending_chars = 0
    flush()
    return merged, sources


def _chunks(eligible: list[TranscriptSegment], config: LabConfig, *, closed_only=False) -> list[tuple]:
    chunks = build_semantic_chunks(eligible, config.chunk_seconds, config.overlap_seconds)
    if closed_only:
        # Decide closure before keyword filtering: the last *selected* chunk
        # may already be closed even when later chunks contain no keywords.
        chunks = chunks[:-1]
    if config.use_keyword_prefilter:
        groups = {"custom": {"user": config.keywords}} if config.keywords else None
        windows = merge_candidate_windows(eligible, find_all_trigger_matches(eligible, groups))
        chunks = [chunk for chunk in chunks if any(
            chunk[-1].end_ms >= window.start_ms and chunk[0].start_ms <= window.end_ms
            for window in windows
        )]
    return chunks


def prepare_snapshot(
    store: VideoStore, video_id: int, config: LabConfig, profile: ProviderProfile,
    *, bundle: dict | None = None, allow_empty: bool = False,
    segments: list[TranscriptSegment] | None = None,
    transcription_model: str | None = None,
    closed_chunks_only: bool = False,
) -> dict:
    video = store.get_video(video_id)
    if video is None:
        raise ValueError("Video does not exist")
    segments = store.get_segments(video_id) if segments is None else segments
    if not segments:
        raise ValueError("Saved ASR transcript is required; this module never runs ASR")
    frozen_profile = profile.model_dump()
    frozen_profile.update(max_tokens=config.max_tokens, max_retries=0)
    profile = ProviderProfile.model_validate(frozen_profile)
    bundle = bundle or load_prompt_bundle()
    transcript = {
        "video_id": video_id, "source_content_hash": video["content_hash"],
        "duration_ms": video["duration_ms"],
        "model_id": transcription_model or video["transcription_model"],
        "segments": [asdict(segment) for segment in segments],
    }
    eligible = _eligible_segments(segments, config)
    input_segments, source_ids = _build_input_view(segments, eligible, config)
    eligible_ids = {segment.id for segment in eligible}
    input_eligible = [segment for segment in input_segments
                      if all(source_id in eligible_ids for source_id in source_ids[segment.id])]
    chunks = _chunks(input_eligible, config, closed_only=closed_chunks_only)
    if not chunks and not allow_empty:
        raise ValueError("No input chunks remain after filtering")
    input_view = [
        {"id": segment.id, "start_ms": segment.start_ms, "end_ms": segment.end_ms,
         "text": segment.text, "source_sentence_ids": source_ids[segment.id]}
        for segment in input_segments
    ]
    snapshot = {
        "pipeline_version": PIPELINE_VERSION, "video_id": video_id,
        "runner_code_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "transcript_hash": digest(transcript), "transcript": transcript,
        "input_segments": input_view, "input_view_hash": digest(input_view),
        "input_stats": {
            "source_segment_count": len(segments),
            "eligible_source_segment_count": len(eligible),
            "input_segment_count": len(input_segments),
            "eligible_input_segment_count": len(input_eligible),
            "prompt_segment_occurrences": sum(len(chunk) for chunk in chunks),
        },
        "config": config.model_dump(), "profile": frozen_profile, "prompt_bundle": bundle,
        "requests": [
            {
                "chunk_id": f"extract-{index:03d}",
                "sentence_ids": [segment.id for segment in chunk],
                "request": request_body(profile, config, render_messages(bundle, config, list(chunk))),
            }
            for index, chunk in enumerate(chunks, 1)
        ],
    }
    if config.match_evidence_quotes:
        snapshot["citation_matching_code_hash"] = hashlib.sha256(
            Path(__file__).with_name("citation_matching.py").read_bytes()
        ).hexdigest()
    snapshot["snapshot_hash"] = digest(snapshot)
    return snapshot


def require_approval(snapshot: dict, approved_hash: str | None) -> None:
    unsigned = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if digest(unsigned) != snapshot["snapshot_hash"]:
        raise ValueError("Experiment snapshot was changed")
    if snapshot["runner_code_hash"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("Experiment implementation changed; regenerate and review the preview")
    if snapshot.get("citation_matching_code_hash") and snapshot["citation_matching_code_hash"] != hashlib.sha256(
        Path(__file__).with_name("citation_matching.py").read_bytes()
    ).hexdigest():
        raise ValueError("Citation matching implementation changed; regenerate the preview")
    if not approved_hash or approved_hash != snapshot["snapshot_hash"]:
        raise ValueError("Preview and approve this exact snapshot before making LLM requests")


def register_run(store: VideoStore, name: str, snapshot: dict, approved_hash: str) -> str:
    require_approval(snapshot, approved_hash)
    experiment_id = f"exp-{uuid.uuid4().hex}"
    store.save_transcript_snapshot(snapshot["transcript_hash"], snapshot["transcript"])
    metadata = {key: value for key, value in snapshot.items() if key != "transcript"}
    store.create_experiment(
        experiment_id, snapshot["video_id"], name, snapshot["config"]["scheme"],
        f"experiment:{experiment_id}", json.dumps(metadata, ensure_ascii=False),
    )
    return experiment_id


def _serialize(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if hasattr(value, "__dict__"):
        return _serialize(vars(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.casefold() in {
                    "authorization", "api_key", "cookie", "set-cookie",
                } else _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


class LoggedClient:
    def __init__(
        self, store: VideoStore, experiment_id: str, profile: ProviderProfile,
        config: LabConfig, api_key: str, *, client=None,
        should_cancel: Callable[[], bool] | None = None,
        call_metadata: dict | None = None,
    ):
        self.store, self.experiment_id = store, experiment_id
        self.config, self.profile, self.secret = config, profile, api_key
        self.should_cancel = should_cancel or (lambda: False)
        self.call_metadata = call_metadata or {}
        self.owns_client = client is None
        if client is None:
            profile.require_ready(api_key)
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key, base_url=profile.base_url,
                timeout=profile.timeout_seconds, max_retries=0,
            )
        # Real clients must not perform hidden retries outside our request log.
        if getattr(client, "max_retries", 0):
            raise ValueError("Experiment client must disable SDK automatic retries")
        self.client = client

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def check_cancelled(self) -> None:
        if self.should_cancel():
            raise ProcessingCancelled("Experiment cancelled")

    def request(
        self, body: dict, chunk_id: str, stage: str, parent: str | None = None,
        *, repair_round: int = 0, evidence_round: int = 0,
    ) -> tuple[str, str]:
        retries = 0
        attempt = 0
        retry_reason = None
        while True:
            self.check_cancelled()
            attempt += 1
            call_id = uuid.uuid4().hex
            record = {
                **self.call_metadata,
                "call_id": call_id, "chunk_id": chunk_id, "stage": stage,
                "parent_call_id": parent, "attempt": attempt,
                "repair_round": repair_round, "retry_reason": retry_reason,
                "evidence_round": evidence_round,
                "request": body, "endpoint": f"{self.profile.base_url}/chat/completions",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "started", "response": None, "raw_content": None,
                "usage": None, "finish_reason": None, "returned_model": None,
                "response_id": None, "elapsed_seconds": None, "error": None,
            }
            self.store.save_experiment_call(self.experiment_id, _redact(record, self.secret))
            started = time.perf_counter()
            error = None
            try:
                response = self.client.chat.completions.create(**body)
                raw = _serialize(response)
                record["response"] = raw
                choice = response.choices[0]
                content = choice.message.content
                record.update(
                    raw_content=_serialize(content), finish_reason=getattr(choice, "finish_reason", None),
                    usage=_serialize(getattr(response, "usage", None)),
                    response_id=getattr(response, "id", None),
                    returned_model=getattr(response, "model", None), status="completed",
                )
                if record["finish_reason"] == "length":
                    record["status"] = "truncated"
                    raise RuntimeError("LLM output was truncated; raw response retained, not treated as no findings")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    raise ValueError("LLM returned non-text content")
            except Exception as exc:
                error = exc
                if record["status"] != "truncated":
                    record["status"] = "error"
                record["error"] = {
                    "type": type(exc).__name__, "message": str(exc),
                    "status_code": getattr(exc, "status_code", None),
                }
                error_response = getattr(exc, "response", None)
                record["error"]["response_body"] = _serialize(getattr(exc, "body", None))
                if error_response is not None:
                    record["error"]["response_text"] = getattr(error_response, "text", None)
            record.update(
                elapsed_seconds=round(time.perf_counter() - started, 6),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            self.store.save_experiment_call(self.experiment_id, _redact(record, self.secret))
            self.check_cancelled()
            if error is None:
                return content, call_id
            parent = call_id
            if "response_format" in body and _response_format_unsupported(error):
                body = {key: value for key, value in body.items() if key != "response_format"}
                retry_reason = "response_format_fallback"
                continue
            status = getattr(error, "status_code", None)
            transient = status == 429 or (isinstance(status, int) and status >= 500) or (
                type(error).__name__ in {"APIConnectionError", "APITimeoutError"}
                or isinstance(error, (ConnectionError, TimeoutError))
            )
            if transient and retries < self.config.request_retries:
                retries += 1
                retry_reason = "transient_error"
                time.sleep(min(0.25 * 2 ** (retries - 1), 2))
                continue
            raise error


def _check_format(raw: str) -> tuple[list | None, list[dict]]:
    try:
        document = json.loads(raw)
    except ValueError as error:
        return None, [{"loc": [], "type": "json_invalid", "msg": str(error)}]
    if document == {"repair_failed": True}:
        return None, [{"loc": [], "type": "repair_declined", "msg": "Model cannot safely repair this output"}]
    items = document.get("candidates") if isinstance(document, dict) else None
    items = items if isinstance(items, list) else None
    try:
        LabResponse.model_validate(document)
    except ValidationError as error:
        return items, _serialize(error.errors(include_url=False, include_input=False))
    return items, []


def validate_candidates(items: list, provided: list[TranscriptSegment], full: list[TranscriptSegment]) -> tuple[list, list]:
    available = {segment.id: segment for segment in provided}
    positions = {segment.id: index for index, segment in enumerate(full)}
    accepted, rejected = [], []
    for item in items:
        try:
            candidate = LabCandidate.model_validate(item)
            first, last = candidate.start_sentence_id, candidate.end_sentence_id
            if first not in available or last not in available or positions[first] > positions[last]:
                raise ValueError("Unknown or reversed sentence range")
            selected = full[positions[first]:positions[last] + 1]
            if any(segment.id not in available for segment in selected):
                raise ValueError("Range crosses a missing transcript span")
            evidence_ids = candidate.evidence_sentence_ids
            if evidence_ids != sorted(set(evidence_ids), key=lambda i: positions.get(i, -1)):
                raise ValueError("Evidence sentence IDs must be ordered and unique")
            if any(i not in {segment.id for segment in selected} for i in evidence_ids):
                raise ValueError("Evidence must be inside the selected range")
            quote = re.sub(r"\s+", "", candidate.evidence_quote)
            evidence = re.sub(r"\s+", "", "".join(available[i].text for i in evidence_ids))
            if not quote or quote not in evidence:
                raise ValueError("Evidence quote does not occur in the referenced ASR sentences")
            if selected[-1].end_ms <= selected[0].start_ms:
                raise ValueError("Invalid timestamp range")
            accepted.append(candidate.model_dump())
        except ValueError as error:
            rejected.append({"candidate": item, "reason": str(error)})
    return accepted, rejected


def evidence_feedback(candidate: dict, reason: str, provided: list[TranscriptSegment]) -> dict:
    """Diagnose a rejected citation without accepting or rewriting it."""
    normalize = lambda text: re.sub(r"\s+", "", text)
    available = {segment.id: segment for segment in provided}
    ids = candidate.get("evidence_sentence_ids", [])
    cited = "".join(available[i].text for i in ids if i in available)
    quote = candidate.get("evidence_quote", "")
    all_text = "".join(normalize(segment.text) for segment in provided)
    position = all_text.find(normalize(quote)) if normalize(quote) else -1
    matches, offset = [], 0
    for segment in provided:
        length = len(normalize(segment.text))
        if position >= 0 and offset < position + len(normalize(quote)) and offset + length > position:
            matches.append(segment.id)
        offset += length
    differences = [
        {
            "operation": operation,
            "referenced_text": normalize(cited)[a:b][:120],
            "model_quote": normalize(quote)[c:d][:120],
        }
        for operation, a, b, c, d in SequenceMatcher(
            None, normalize(cited), normalize(quote), autojunk=False,
        ).get_opcodes() if operation != "equal"
    ]
    return {
        "code": "evidence_validation_failed", "reason": reason,
        "instruction": "逐字核对引文、全部相关编号及起止范围；不改写教师原话。",
        "declared_evidence_sentence_ids": ids,
        "referenced_sentences": [{"id": i, "text": available[i].text}
                                 for i in ids if i in available],
        "unknown_evidence_sentence_ids": [i for i in ids if i not in available],
        "submitted_quote": quote,
        "exact_quote_found_in_supplied_transcript": position >= 0,
        "exact_match_sentence_ids": matches,
        "missing_from_declared_ids": [i for i in matches if i not in ids],
        "outside_selected_range": [
            i for i in matches
            if not candidate.get("start_sentence_id", 0) <= i <= candidate.get("end_sentence_id", 0)
        ],
        "text_differences": differences[:12],
    }


def rematch_rejections(
    rejected: list[dict], provided: list[TranscriptSegment], full: list[TranscriptSegment],
) -> tuple[list, list, list]:
    from citation_matching import remap_candidate

    recovered, unresolved, audits = [], [], []
    for rejection in rejected:
        candidate = rejection["candidate"]
        try:
            LabCandidate.model_validate(candidate)
            corrected, audit = remap_candidate(candidate, provided)
            if corrected is not None:
                accepted, failed = validate_candidates([corrected], provided, full)
                if accepted:
                    recovered.append({
                        **accepted[0], "model_evidence_quote": candidate["evidence_quote"],
                        "citation_match_method": audit["method"],
                    })
                else:
                    corrected = None
                    audit.update(status="needs_review", reason=failed[0]["reason"])
            if corrected is None:
                unresolved.append(rejection)
            audits.append({**audit, "original_rejection_reason": rejection["reason"]})
        except ValueError as error:
            unresolved.append(rejection)
            audits.append({"status": "unmatched", "reason": str(error), "original_candidate": candidate})
    return recovered, unresolved, audits


def render_evidence_repair(
    bundle: dict, config: LabConfig, supplied: list[TranscriptSegment],
    rejection: dict, *, backend_audit: dict | None = None,
    invalid_output: str | None = None, format_errors: list | None = None,
) -> list[dict]:
    sections = [
        bundle["rules"], bundle["evidence_repair"],
        _data_block("original_candidate_json", rejection["candidate"]),
        _data_block("evidence_errors_json", evidence_feedback(
            rejection["candidate"], rejection["reason"], supplied,
        )),
    ]
    if backend_audit:
        sections.append(_data_block("backend_matching_json", backend_audit))
    if invalid_output is not None:
        sections += [
            _data_block("invalid_output_json", invalid_output),
            _data_block("format_errors_json", format_errors or []),
        ]
    sections.append(_transcript_block(supplied))
    return [
        {"role": "system", "content": bundle["system"]},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def repair_evidence(
    logged: LoggedClient, bundle: dict, rejected: list[dict],
    supplied: list[TranscriptSegment], full: list[TranscriptSegment],
    step_id: str, parent: str | None, *, backend_audits: list[dict] | None = None,
) -> tuple[list, list]:
    """Repair only unresolved citations, with independent evidence/JSON budgets."""
    config = logged.config
    if not config.max_evidence_repairs or not rejected:
        return [], rejected
    recovered, unresolved = [], []
    semantic_fields = ("level", "signal_type", "title", "reason")
    for index, original in enumerate(rejected, 1):
        child_id = f"{step_id}-evidence-{index:03d}"
        audit = next((item for item in backend_audits or []
                      if item.get("original_candidate") == original["candidate"]), None)
        step = {
            "step_id": child_id, "stage": "evidence_repair", "status": "processing",
            "call_id": None, "source_call_id": parent,
            "first_rejected": [original], "accepted": [], "rejected": [original],
            "provided_sentence_ids": [segment.id for segment in supplied],
            "first_pass_format_valid": None, "final_format_valid": None,
            "format_attempts": [], "repair_count": 0,
            "max_format_repairs": config.max_format_repairs if config.repair_json else 0,
            "repair_limit_reached": False, "raw_candidate_count": None,
            "evidence_attempts": [], "evidence_repair_count": 0,
            "max_evidence_repairs": config.max_evidence_repairs,
            "evidence_limit_reached": False,
        }
        pending = original
        call_parent = parent
        store = logged.store
        store.save_experiment_step(logged.experiment_id, child_id, _redact(step, logged.secret))
        fixed = None
        try:
            for evidence_round in range(1, config.max_evidence_repairs + 1):
                step["evidence_repair_count"] = evidence_round
                messages = render_evidence_repair(bundle, config, supplied, pending, backend_audit=audit)
                errors = []
                items = None
                original_count = None
                protected = []
                for format_round in range(step["max_format_repairs"] + 1):
                    raw, call_id = logged.request(
                        request_body(logged.profile, config, messages), child_id,
                        "json_repair" if format_round else "evidence_repair", call_parent,
                        repair_round=format_round, evidence_round=evidence_round,
                    )
                    call_parent = call_id
                    step["call_id"] = call_id
                    items, errors = _check_format(raw)
                    step["final_format_valid"] = not errors
                    step["raw_candidate_count"] = len(items) if items is not None else None
                    if step["first_pass_format_valid"] is None:
                        step["first_pass_format_valid"] = not errors
                    step["format_attempts"].append({
                        "call_id": call_id, "repair_round": format_round,
                        "evidence_round": evidence_round, "format_valid": not errors,
                        "errors": errors, "raw_candidate_count": step["raw_candidate_count"],
                    })
                    if not format_round:
                        original_count = step["raw_candidate_count"]
                        for item in items or []:
                            try:
                                LabCandidate.model_validate(item)
                                protected.append(item)
                            except ValueError:
                                pass
                    store.save_experiment_step(logged.experiment_id, child_id, _redact(step, logged.secret))
                    if not errors or any(error["type"] == "repair_declined" for error in errors):
                        break
                    if format_round == step["max_format_repairs"]:
                        step["repair_limit_reached"] = True
                        break
                    messages = render_evidence_repair(
                        bundle, config, supplied, pending, backend_audit=audit,
                        invalid_output=raw, format_errors=errors,
                    )
                if errors:
                    step["evidence_attempts"].append({
                        "evidence_round": evidence_round, "call_id": call_parent,
                        "status": "format_failed", "errors": errors,
                    })
                    break
                if format_round and (
                    (original_count is not None and len(items) != original_count)
                    or any(items.count(item) < protected.count(item) for item in protected)
                ):
                    step["error"] = "Format repair changed or removed candidates"
                    break
                if len(items) != 1 or any(
                    items[0][key] != original["candidate"][key] for key in semantic_fields
                ):
                    step["error"] = "Evidence repair changed candidate count or protected semantic fields"
                    break
                accepted, failed = validate_candidates(items, supplied, full)
                matches = []
                if failed and config.match_evidence_quotes:
                    matched, failed, matches = rematch_rejections(failed, supplied, full)
                    accepted.extend(matched)
                step["evidence_attempts"].append({
                    "evidence_round": evidence_round, "call_id": call_parent,
                    "status": "accepted" if accepted else "rejected",
                    "candidate": items[0], "rejected": failed, "citation_matches": matches,
                })
                if accepted:
                    fixed = {
                        **accepted[0], "call_id": call_parent,
                        "initial_evidence_candidate": original["candidate"],
                        "evidence_repaired": True,
                    }
                    step.update(accepted=[fixed], rejected=[])
                    break
                pending = failed[0]
                audit = matches[0] if matches else None
            if fixed is None:
                step["evidence_limit_reached"] = (
                    len(step["evidence_attempts"]) >= config.max_evidence_repairs
                    and not step.get("error")
                    and step["evidence_attempts"][-1]["status"] == "rejected"
                )
                unresolved.append(original)
            else:
                recovered.append(fixed)
            step["status"] = "completed" if fixed else "needs_review"
        except ProcessingCancelled:
            step["status"] = "interrupted"
            raise
        except Exception as error:
            step.update(status="needs_review", error=str(error))
            unresolved.append(original)
        finally:
            calls = [call for call in store.list_experiment_calls(logged.experiment_id)
                     if call["chunk_id"] == child_id]
            step["repair_count"] = len({
                (call["evidence_round"], call["repair_round"]) for call in calls
                if call["repair_round"] > 0
            })
            store.save_experiment_step(logged.experiment_id, child_id, _redact(step, logged.secret))
    return recovered, unresolved


def _deduplicate(candidates: list[dict], by_id: dict[int, TranscriptSegment]) -> list[dict]:
    groups: list[dict] = []
    for item in sorted(candidates, key=lambda c: by_id[c["start_sentence_id"]].start_ms):
        start, end = by_id[item["start_sentence_id"]].start_ms, by_id[item["end_sentence_id"]].end_ms
        group = next((g for g in groups if (
            start < g["end_ms"] and end > g["start_ms"]
            and set(item["evidence_sentence_ids"]) & set(g["evidence_sentence_ids"])
            and re.sub(r"\s+", "", item["title"]) == re.sub(r"\s+", "", g["title"])
        )), None)
        if group is None:
            groups.append({
                **item, "start_ms": start, "end_ms": end, "source_quote": item["evidence_quote"],
                "members": [item], "deduplication_rule": "overlap+shared-evidence+same-title-v1",
            })
        else:
            group["start_ms"] = min(start, group["start_ms"])
            group["end_ms"] = max(end, group["end_ms"])
            group["evidence_sentence_ids"] = sorted(
                set(group["evidence_sentence_ids"]) | set(item["evidence_sentence_ids"]),
                key=lambda i: by_id[i].start_ms,
            )
            if start < by_id[group["start_sentence_id"]].start_ms:
                group["start_sentence_id"] = item["start_sentence_id"]
            if end > by_id[group["end_sentence_id"]].end_ms:
                group["end_sentence_id"] = item["end_sentence_id"]
            group["members"].append(item)
    return groups


def _run_report(
    store: VideoStore, experiment_id: str, snapshot: dict, collected: list,
    technical_failures: list, results: list, status: str, *, complete: bool,
) -> dict:
    calls = store.list_experiment_calls(experiment_id)
    all_steps = store.list_experiment_steps(experiment_id)
    steps = [step for step in all_steps if "format_attempts" in step]

    def format_metrics(selected: list[dict]) -> dict:
        evaluated = [step for step in selected if step["first_pass_format_valid"] is not None]
        first_valid = sum(step["first_pass_format_valid"] is True for step in evaluated)
        return {
            "step_count": len(selected), "evaluated_step_count": len(evaluated),
            "unassessed_step_count": len(selected) - len(evaluated),
            "first_pass_valid_count": first_valid,
            "first_pass_invalid_count": len(evaluated) - first_valid,
            "first_pass_valid_rate": first_valid / len(evaluated) if evaluated else None,
            "final_valid_count": sum(step["final_format_valid"] is True for step in selected),
            "final_invalid_count": sum(step["final_format_valid"] is False for step in selected),
            "recovered_format_step_count": sum(
                step["first_pass_format_valid"] is False and step["final_format_valid"] is True
                for step in selected
            ),
            "repair_count": sum(step["repair_count"] for step in selected),
            "repair_limit_reached_count": sum(step["repair_limit_reached"] for step in selected),
        }

    usage = {"reported_calls": sum(call["usage"] is not None for call in calls), "totals": {}}
    for call in calls:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = (call["usage"] or {}).get(key)
            if isinstance(value, int):
                usage["totals"][key] = usage["totals"].get(key, 0) + value
    metrics = format_metrics(steps)
    metrics["by_stage"] = {
        stage: format_metrics([step for step in steps if step["stage"] == stage])
        for stage in ("extract", "review", "evidence_repair")
    }
    extraction_steps = [step for step in all_steps if step["stage"] != "evidence_repair"]
    evidence_steps = [step for step in steps if step["stage"] == "evidence_repair"]
    citation_metrics = {
        "initial_rejected_count": sum(
            len(step.get("first_rejected", [])) for step in extraction_steps
        ),
        "matched_count": sum(
            match.get("status") == "matched"
            for step in extraction_steps for match in step.get("citation_matches", [])
        ),
        "unresolved_count": sum(len(step.get("rejected", [])) for step in extraction_steps),
        "additional_api_calls": sum(call.get("evidence_round", 0) > 0 for call in calls),
        "model_recovered_count": sum(len(step.get("accepted", [])) for step in evidence_steps),
        "evidence_repair_count": sum(step["evidence_repair_count"] for step in evidence_steps),
        "evidence_limit_reached_count": sum(step["evidence_limit_reached"] for step in evidence_steps),
    }
    return {
        "snapshot_hash": snapshot["snapshot_hash"], "transcript_hash": snapshot["transcript_hash"],
        "input_view_hash": snapshot["input_view_hash"], "input_stats": snapshot["input_stats"],
        "status": status, "complete": complete,
        "results": results, "pre_merge_candidates": collected,
        "technical_failures": technical_failures, "format_metrics": metrics,
        "format_repair_count": metrics["repair_count"],
        "citation_metrics": citation_metrics,
        "format_repair_api_call_count": sum(call["stage"] == "json_repair" for call in calls),
        "transport_retry_count": sum(call.get("retry_reason") == "transient_error" for call in calls),
        "response_format_fallback_count": sum(
            call.get("retry_reason") == "response_format_fallback" for call in calls
        ),
        "candidate_count": len(results) if complete else None,
        "call_count": len(calls), "usage": usage,
        "latency_seconds": sum(call["elapsed_seconds"] or 0 for call in calls),
        "accuracy": None, "cost": None,
    }


def execute_run(
    store: VideoStore, experiment_id: str, snapshot: dict, api_key: str,
    approved_hash: str, *, client=None, progress=None, should_cancel=None,
    early_extraction=None, chunk_progress=None,
) -> dict:
    require_approval(snapshot, approved_hash)
    if snapshot.get("operation") == "offline_citation_rematch":
        raise ValueError("Offline citation rematching must not execute LLM requests")
    row = store.get_experiment(experiment_id)
    if row is None or json.loads(row["options_json"]).get("snapshot_hash") != approved_hash:
        raise ValueError("Run is not bound to this approved snapshot")
    if row["status"] != "queued":
        raise ValueError("Each experiment can execute only once; create a new run")
    store.update_experiment(experiment_id, status="processing")
    logged = None
    collected = []
    technical_failures = []
    try:
        config = LabConfig.model_validate(snapshot["config"])
        profile = ProviderProfile.model_validate(snapshot["profile"])
        full = [
            TranscriptSegment(segment["id"], segment["start_ms"], segment["end_ms"], segment["text"])
            for segment in snapshot["input_segments"]
        ]
        source_ids = {segment["id"]: segment["source_sentence_ids"] for segment in snapshot["input_segments"]}
        by_id = {segment.id: segment for segment in full}
        bundle = snapshot["prompt_bundle"]
        logged = LoggedClient(store, experiment_id, profile, config, api_key,
                              client=client, should_cancel=should_cancel)

        def select(body: dict, step_id: str, stage: str, supplied: list, parent=None) -> tuple[list, str]:
            limit = config.max_format_repairs if config.repair_json else 0
            step = {
                "step_id": step_id, "stage": stage, "call_id": None, "status": "processing",
                "first_pass_format_valid": None, "final_format_valid": None,
                "format_attempts": [], "repair_count": 0, "max_format_repairs": limit,
                "repair_limit_reached": False, "raw_candidate_count": None,
                "accepted": [], "rejected": [],
                "provided_sentence_ids": [segment.id for segment in supplied],
            }
            store.save_experiment_step(experiment_id, step_id, step)
            repair_round = 0
            original_count = None
            protected_candidates = []
            try:
                while True:
                    prefetched = (
                        early_extraction.take(snapshot, step_id)
                        if early_extraction is not None and stage == "extract" and repair_round == 0
                        else None
                    )
                    if prefetched is not None:
                        raw, call_id = prefetched
                    else:
                        raw, call_id = logged.request(
                            body, step_id, stage if repair_round == 0 else "json_repair",
                            parent, repair_round=repair_round,
                        )
                    items, errors = _check_format(raw)
                    valid = not errors
                    step["call_id"] = call_id
                    step["final_format_valid"] = valid
                    step["raw_candidate_count"] = len(items) if items is not None else None
                    step["format_attempts"].append({
                        "call_id": call_id, "repair_round": repair_round,
                        "format_valid": valid, "errors": errors,
                        "raw_candidate_count": step["raw_candidate_count"],
                    })
                    if repair_round == 0:
                        step["first_pass_format_valid"] = valid
                        original_count = step["raw_candidate_count"]
                        for item in items or []:
                            try:
                                LabCandidate.model_validate(item)
                                protected_candidates.append(item)
                            except ValueError:
                                pass
                    store.save_experiment_step(experiment_id, step_id, _redact(step, api_key))
                    if valid:
                        # Formatting repairs must not erase findings or rewrite already valid candidates.
                        if repair_round and (
                            (original_count is not None and len(items) != original_count)
                            or (original_count is None and not items)
                            or any(
                                items.count(item) < protected_candidates.count(item)
                                for item in protected_candidates
                            )
                        ):
                            raise ValueError("Format repair changed or removed candidates; manual review required")
                        accepted, rejected = validate_candidates(items, supplied, full)
                        step["first_rejected"] = rejected
                        if config.match_evidence_quotes:
                            recovered, rejected, matches = rematch_rejections(rejected, supplied, full)
                            accepted.extend(recovered)
                            step["citation_matches"] = matches
                        repaired, rejected = repair_evidence(
                            logged, bundle, rejected, supplied, full, step_id, call_id,
                            backend_audits=step.get("citation_matches"),
                        )
                        accepted.extend(repaired)
                        step.update(status="completed", accepted=accepted, rejected=rejected)
                        if rejected:
                            technical_failures.append({"step_id": step_id, "rejected_candidates": len(rejected)})
                        return accepted, call_id
                    if repair_round and any(error["type"] == "repair_declined" for error in errors):
                        raise ValueError("Model declined format repair; manual review required")
                    if repair_round >= limit:
                        step["repair_limit_reached"] = True
                        raise ValueError(
                            f"Format validation failed for {step_id} after {repair_round} repairs "
                            f"(limit {limit}): {json.dumps(errors, ensure_ascii=False)}"
                        )
                    repair_round += 1
                    repair = render_messages(
                        bundle, config, supplied, repair_output=raw, repair_errors=errors,
                    )
                    body = request_body(profile, config, repair)
                    parent = call_id
            except BaseException as error:
                step.update(
                    status="interrupted" if isinstance(error, ProcessingCancelled) else "failed",
                    error=str(error),
                )
                raise
            finally:
                step["repair_count"] = len({
                    call["repair_round"] for call in store.list_experiment_calls(experiment_id)
                    if call["chunk_id"] == step_id and call.get("repair_round", 0) > 0
                })
                store.save_experiment_step(experiment_id, step_id, _redact(step, api_key))

        if snapshot.get("operation") == "evidence_repair_replay":
            collected.extend(snapshot["source_pre_merge_candidates"])
        def notify_chunk(completed, current):
            if chunk_progress is not None:
                # Only evidence-checked candidates leave this boundary, never raw model output.
                try:
                    chunk_progress(completed=completed, total=len(snapshot["requests"]), current=current,
                                   results=_redact(_deduplicate(collected, by_id), api_key))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Chunk preview unavailable")

        notify_chunk(0, 1 if snapshot["requests"] else None)
        for index, prepared in enumerate(snapshot["requests"]):
            logged.check_cancelled()
            if index:
                notify_chunk(index, index + 1)
            chunk_id = prepared["chunk_id"]
            supplied = [by_id[i] for i in prepared["sentence_ids"]]
            if snapshot.get("operation") == "evidence_repair_replay":
                original = prepared["rejected"]
                selected, rejected, audits = rematch_rejections(original, supplied, full)
                call_id = prepared["source_call_id"]
                repaired, rejected = repair_evidence(
                    logged, bundle, rejected, supplied, full, chunk_id, call_id,
                    backend_audits=audits,
                )
                selected.extend(repaired)
                store.save_experiment_step(experiment_id, chunk_id, {
                    "step_id": chunk_id, "stage": "citation_replay", "status": "completed",
                    "source_call_id": call_id, "first_rejected": original,
                    "accepted": selected, "rejected": rejected, "citation_matches": audits,
                })
                if rejected:
                    technical_failures.append({"step_id": chunk_id, "rejected_candidates": len(rejected)})
            else:
                selected, call_id = select(prepared["request"], chunk_id, "extract", supplied)
            for candidate_index, candidate in enumerate(selected, 1):
                if config.scheme == "context_review":
                    left = by_id[candidate["start_sentence_id"]].start_ms - config.review_context_seconds * 1000
                    right = by_id[candidate["end_sentence_id"]].end_ms + config.review_context_seconds * 1000
                    context = [segment for segment in full if segment.end_ms >= left and segment.start_ms <= right]
                    step_id = f"{chunk_id}-review-{candidate_index:03d}"
                    review = render_messages(bundle, config, context, candidate=candidate)
                    checked, review_call_id = select(
                        request_body(profile, config, review), step_id, "review", context, call_id,
                    )
                    if len(checked) > 1 or any(
                        by_id[c["end_sentence_id"]].end_ms <= by_id[candidate["start_sentence_id"]].start_ms
                        or by_id[c["start_sentence_id"]].start_ms >= by_id[candidate["end_sentence_id"]].end_ms
                        for c in checked
                    ):
                        raise ValueError("Review added unrelated candidates; inspect the saved response")
                    for checked_candidate in checked:
                        collected.append({**checked_candidate, "origin_chunk_id": chunk_id,
                                          "call_id": review_call_id, "initial_candidate": candidate})
                else:
                    collected.append({**candidate, "origin_chunk_id": chunk_id,
                                      "call_id": candidate.get("call_id", call_id)})
            notify_chunk(index + 1, None)
            if progress:
                progress((index + 1) / len(snapshot["requests"]), f"Experiment chunks {index + 1}/{len(snapshot['requests'])}")
        results = _deduplicate(collected, by_id)
        positions = {segment.id: index for index, segment in enumerate(full)}
        for result in results:
            selected = full[positions[result["start_sentence_id"]]:positions[result["end_sentence_id"]] + 1]
            result["source_sentence_ids"] = [
                source_id for segment in selected for source_id in source_ids[segment.id]
            ]
            result["source_start_sentence_id"] = result["source_sentence_ids"][0]
            result["source_end_sentence_id"] = result["source_sentence_ids"][-1]
        status = "completed_with_validation_errors" if technical_failures else "completed"
        report = _run_report(
            store, experiment_id, snapshot, collected, technical_failures, results, status, complete=True,
        )
        store.save_experiment_results(experiment_id, _redact(report, api_key))
        store.update_experiment(experiment_id, status=status, candidate_count=len(results), error_message="")
        return report
    except BaseException as error:
        message = str(_redact(str(error), api_key))
        status = "interrupted" if isinstance(error, ProcessingCancelled) else "failed"
        report = _run_report(
            store, experiment_id, snapshot, collected, technical_failures, [], status, complete=False,
        )
        report["error"] = message
        store.save_experiment_results(experiment_id, _redact(report, api_key))
        store.update_experiment(
            experiment_id,
            status=status,
            error_message=message,
        )
        from api_failure import ApiUnavailable, api_failure

        if message != str(error):
            failure = api_failure(error)
            if failure is not None:
                raise ApiUnavailable(message, status_code=getattr(failure, "status_code", None)) from None
            raise RuntimeError(message) from None
        raise
    finally:
        if logged is not None:
            logged.close()
