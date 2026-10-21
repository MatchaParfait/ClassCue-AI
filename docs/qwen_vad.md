# Optional FireRedVAD Boundaries for Qwen

The existing application transcription defaults are unchanged. The standalone
chunked CLI still defaults to fixed 300-second chunks and batch size 1.
Pyannote integration is deferred because its official checkpoint requires access
approval; it is not installed or offered as a selectable backend.

## Commands

Traditional fixed boundaries:

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT
```

Qwen-style low-energy boundaries, approximately 90 seconds, two chunks per batch:

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT --chunk-seconds 90 --batch-size 2 --boundary-mode energy
```

Optional CPU FireRedVAD assistance:

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT --chunk-seconds 90 --batch-size 2 --boundary-mode firered
```

Use a separate output directory per mode. This CLI supports `--max-chunks` for
smoke tests. Limited runs are partial transcripts, not complete file coverage.
`chunk_plan.json` records the full source plan and VAD parameters.

## Boundary Rules

- Search within five seconds of each target.
- Energy mode minimizes a 100 ms sliding absolute-amplitude window, then selects
  its minimum-amplitude sample. A cumulative sum replaces the official NumPy
  convolution for linear-time computation; floating-point ties can differ.
- FireRed mode first seeks a detected non-speech gap of at least 300 ms, with
  100 ms safety margins. Among candidates, select the closest gap midpoint,
  preferring wider gaps on ties, then apply energy search inside that gap.
- If no sufficiently wide gap exists, use energy search in the original window.
- Empty VAD detections also use energy fallback; unavailable models raise an
  error rather than silently claiming VAD ran.
- Without `--skip-non-speech`, every source sample is retained exactly once.
- Chunk offsets and aligned word times are restored to the source timeline.
- Approximately 90 seconds means actual full chunks can be 85 to 95 seconds.

## FireRed Runtime

The CPU-only VAD subprocess reuses the existing `firered-asr2` environment and
local `fireredasr2-aed/vad/VAD` weights. No FireRed ASR model is loaded.
`--vad-python` and `--vad-model-path` override these paths.

Inference uses 60-second windows with two seconds of context on both sides,
retains each probability frame once, and runs the official postprocessor
globally. The official forced short-segment setting is disabled so it cannot
manufacture pauses for the boundary planner.

FireRed's silence-confirmation state machine can shorten the returned gap
relative to the acoustic pause; selection is conservative and may fall back.
The two-thread VAD process finishes before the Qwen GPU worker is started.

## Reproducible Comparison

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_qwen_vad.py VIDEO --output REPORT --modes energy firered --sample-start 600 --sample-seconds 360 --chunk-seconds 90 --batch-size 2
```

The benchmark detects boundaries over the full audio, then compares Qwen on the
same excerpt. Full-file detection speed and excerpt ASR speed must not be
presented as a full-file end-to-end ASR measurement.

Outputs include full plans, worker load/inference times, excerpt transcripts,
ASR/alignment stage times, PyTorch allocator peaks, and sampled device memory.
Model loading and warmup are excluded from measured excerpt Qwen time.
The memory sampler is approximate, not an exact device peak measurement.

For a second run with reversed order, use the Qwen runtime Python:

```powershell
QWEN_PYTHON scripts\qwen_benchmark_worker.py --plan REPORT\inference_plan.json --output REPORT\repeat --reverse-order
```

These timings measure performance, not recognition accuracy. Both clean meeting
and noisy surveillance audio should be checked before enabling VAD by default.

## Optional Non-Speech Skipping

Historical measurements are retained privately, not distributed with this
source repository.
General audio keeps all samples inside external chunks. Only choose the
`mostly-silent` profile when the user explicitly confirms that most of the
recording has no dialogue; noisy audio alone is not a reason to select it.

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT --chunk-seconds 90 --batch-size 2 --boundary-mode firered --audio-profile mostly-silent --staged
```

`--audio-profile general` is the default and does not skip silence. The same
small routing interface is available as `transcribe_qwen(...,
audio_profile="mostly-silent")` or worker
`silence_options={"audio_profile": "mostly-silent"}`. It does not change the
worker's combined model schedule. `resolve_silence_skipping()` in
`qwen_silence.py` makes this decision without any model or automatic scene guess.
Explicit `skip_non_speech` booleans take precedence over the profile.

This is a separate opt-in switch. FireRed boundary assistance alone still retains
all audio. The installed Qwen package and existing application defaults are not
modified.

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT --chunk-seconds 90 --batch-size 2 --boundary-mode firered --skip-non-speech
```

The same full-source CPU detection serves both boundary selection and skipping.
Speech intervals receive 300 ms of context on each side. Only remaining gaps of
at least 1.5 seconds are skipped; shorter pauses stay in the input. These are
configurable via `--speech-padding-seconds` and `--min-removed-seconds`.
Empty full-file detections conservatively retain all audio instead of deleting
the entire source. Missing/invalid VAD results fail explicitly.

External boundaries remain unchanged. Retained sections inside each external
chunk are concatenated before ASR and alignment. Empty chunks are not inferred.
Every retained section has a sample-exact compact-to-source map. Aligned words
are mapped individually, and subtitles are regrouped across restored pauses.
TXT/SRT/JSON timestamps use the original source, not the shortened input.
`silence_plan.json` records removed intervals and the full map.
`mapping_warnings.json` records any aligned token crossing a compacted join.
Such tokens have ambiguous source spans and require listening review.
The source file is never modified.

For the application's Qwen worker, callers can opt in with
`silence_options={"skip_non_speech": True, "chunk_seconds": 90, "batch_size": 2}`
and an optional `audit_output` JSON path. The direct `transcribe_qwen` API accepts
the same keyword options plus a mutable `silence_report` dictionary. No default
or GUI selection has been changed.
This boolean switch is retained for compatibility and controlled experiments.
`--keep-non-speech` / `skip_non_speech=False` explicitly disables compaction even
for `mostly-silent`. The routing choice is recorded in the direct API audit,
chunked CLI metadata, or staged CLI `inference_plan.json`.

## Stable-Memory Staged Mode

On systems with limited Windows commit headroom, use separate sequential model
processes. GPU ASR processes all chunks and saves text, then exits completely.
A new CPU FP32 aligner process performs alignment and source-time restoration.
The two models never reside simultaneously. Models, precision, BS and audio
conditions stay the same; an extra process startup is part of cold-run overhead.

```powershell
.\.venv\Scripts\python.exe scripts\qwen_chunked.py VIDEO --output OUTPUT --chunk-seconds 90 --batch-size 2 --boundary-mode firered --skip-non-speech --staged
```

Omit `--skip-non-speech` to retain silence with the same staged schedule.
This mode currently requires configured FireRed paths and does not support
`--max-chunks`. Outputs are `qwen-skip.*` or `qwen-no-skip.*`, plus plans, logs,
phase timings and memory records. Use a fresh output directory for each run.
The GUI and existing worker API still use their previous combined schedule.

CPU threads default to four (`QWEN_CPU_THREADS` can override). oneDNN primitive
caching is disabled, and unused GPU workspace is released between batches.
Before batches, at least 2 GiB of system physical RAM and 4 GiB of system commit
headroom are required. Staged CLI monitoring stops the isolated worker after two
unsafe samples: private commit over 12 GiB, free physical RAM below 1.5 GiB, or
free commit below 3 GiB. These are safety limits, not a guarantee for every input.

Resident physical RAM, private commit and GPU VRAM are different counters.
Enough free physical RAM does not imply enough Windows commit capacity.
Pagefile/system settings are not changed by these scripts.

## Skipping Comparison And Review

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_qwen_silence.py VIDEO --output REPORT --chunk-seconds 90 --batch-size 2
.\.venv\Scripts\python.exe scripts\review_qwen_silence.py REPORT
.\.venv\Scripts\python.exe scripts\summarize_qwen_silence.py REPORT
```

Add `--staged --cpu-threads 4` for the bounded, sequential model schedule.
ASR/alignment phase times exclude load and warmup; load/startup overhead is
recorded separately. Post-batch and per-second memory records show whether the
process reaches a steady platform. `memory_summary.json` recommends free
physical/commit capacity using observed peaks plus a separate system margin.

The default comparison processes the complete extracted audio twice with identical
FireRed-assisted external cuts. Only non-speech skipping differs. Both conditions
share one VAD pass because both already use it for boundary planning. Warm Qwen
ASR/alignment time and shared VAD overhead are reported separately.
`--audio SOURCE_WAV` reuses a prior extraction; a SHA256 identifies the exact input.
`--sample-start` and `--sample-seconds` allow smaller explicitly labeled experiments.
`--reverse-order` reverses the condition order.

`review/index.html` lists every full-source skipped interval with original
timestamps, shaded source waveforms, original audio with two seconds of context,
the exact skipped audio and the resulting local join. It also shows source-time
transcripts for each condition and flags baseline words overlapping skipped
regions. These flags are review hints, not ground truth: ASR/alignment can itself
be wrong. Choices and notes are browser-local; export CSV for durable annotations.
`review/deleted_regions.csv` and `review/review_data.json` provide machine-readable
records. Context clips are illustrative; joins are assembled from adjacent source
context, not a recording of Qwen's actual tensor input.

Performance does not prove accuracy. Listen especially for quiet/short speech,
word-edge damage, and noisy scenes before turning this option on for bulk jobs.
