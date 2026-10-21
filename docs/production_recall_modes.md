# Three Production Recall Modes

The single-video endpoint, batch endpoint, React UI, Streamlit UI and batch CLI
now select one extraction policy, not three requests followed by voting:

| Mode | Prompt Files | Policy |
| --- | --- | --- |
| `high_recall` | `prompts/exam_lab/` | Exam, emphasis/mastery and easy-error cues |
| `medium_recall` | `prompts/exam_lab/precision/` | Excludes merely local solution guidance |
| `low_recall` | `prompts/exam_lab/low_recall/` | Explicit exam/preparation or strong mastery/memory requirements |

These are policy names, not measured recall or precision guarantees. The default
in the API, React processing pages and CLI is `medium_recall`. The legacy
`strict_exam_intent` API mode and its stored results remain readable.

## Invocation

`ai_provider.create_judge` routes the three modes to `ExamPointJudge`.
The production pipeline delegates a whole saved transcript to the same engine
used in the experiments. The existing adapter remains for legacy callers.

- Scheme B (`few_shot`), 600-second chunks, 30-second overlap.
- At most one in-flight LLM request per run, including repair calls.
- Aggregate to at least 20 non-whitespace characters where possible, maximum
  30-second span and 2-second gap. Original ASR fragments are space-separated.
- Send only `{id} text`; backend metadata restores the first fragment time.
- No model confidence field. The old DB confidence column remains solely for
  compatibility and is not a model-reported probability.
- Provider token limit and timeout remain configurable.
- Optional `reasoning_effort` is saved in AI settings and sent unchanged to every
  extraction/repair request. `null` means provider default. Select `high` to
  reproduce the latest experiment setting on a compatible provider. Unsupported
  reasoning parameters fail visibly; they are not silently dropped.
- Existing filter and keyword options still apply. They can reduce coverage.

Example production request:

```json
{
  "analysis_mode": "medium_recall",
  "filter_formula_noise": false,
  "force_retranscribe": false
}
```

Submit to `POST /api/videos/{video_id}/process` or the existing retry endpoint.
Changing recall mode reuses persisted ASR. CLI example:

```powershell
.\.venv\Scripts\python.exe -m classcue.batch_process --videos "D:\example-videos" --mode low_recall --reasoning-effort high --no-formula-noise-filter
```

## Evidence And Logs

Backend matching handles allowed whitespace/filler normalization first. Only
unresolved evidence is returned to the model with detailed validation feedback.
Limits: one JSON repair per response, two evidence repair rounds per candidate.
Raw replies, usage reported by the API, retry/repair attempts, validation errors,
frozen prompts/configuration and transcript snapshots are retained in the
existing `video_experiments` and `llm_experiment_*` tables.

Production runs are named `production:{mode}` and carry
`authorization.kind = production_processing_request`. Their hash binding is an
integrity check, not a claim that each production request was manually reviewed.
These records appear in the existing experiment/log endpoints. No historical
experiment is promoted automatically.

`GET /api/videos/{video_id}/analysis-runs` includes `experiment_id` and
`analysis_signature` to locate the last successfully published run. Reuse
requires matching transcript, prompt, model and configuration, rather than mode
name alone. Each actual rerun receives a new log record.

Only a completely validated run is atomically published to formal markers.
Failure, cancellation or exhausted evidence repair preserves prior markers.
Different objects at the same timestamp stay separate. Matching mastered and
excluded decisions survive publication; other recall modes are untouched.
Successful reruns replace the current automatic markers for that mode, while
historical model outputs remain in logs.

## Verification

Production adapter tests use mocked clients and temporary databases. They cover
mode selection, ASR reuse, signature invalidation, serial calls, bounded repair,
history retention, same-time objects, review decisions and failure/cancellation.
No paid API call is necessary for these integration tests.
