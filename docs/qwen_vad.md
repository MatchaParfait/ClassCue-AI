# Qwen Silence Handling

Application transcription retains non-speech audio by default. Selecting
FireRed VAD for boundaries does not by itself enable silence removal.

The direct `transcribe_qwen` API and `QwenWorker.transcribe` support an explicit
`audio_profile="mostly-silent"` option. Only use it when the caller has confirmed
that most of the recording has no dialogue; noisy audio is not sufficient.
An explicit boolean `skip_non_speech` overrides the profile. Unknown profiles
and non-boolean overrides fail before transcription.

When enabled, detected speech receives context padding. Short pauses remain,
and empty VAD detections conservatively retain the original audio. A
sample-exact compact-to-source mapping restores word and subtitle timestamps
to the original video timeline. Tokens crossing a compacted join are recorded
as mapping warnings, not silently treated as verified alignment.

Production implementation lives in `classcue/qwen_silence.py`, `classcue/qwen_transcriber.py`,
`classcue/qwen_resilience.py`, and the Qwen workers. Regression coverage lives in
`tests/test_qwen_silence.py` and `tests/test_qwen_silence_routing.py`.
Standalone benchmark, corpus-review, and comparison workflows are not part of
the public application.
