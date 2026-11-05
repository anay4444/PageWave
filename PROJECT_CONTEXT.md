# PageWave shared project context

Last reviewed: 2026-11-05

## What this project is

PageWave is a Windows desktop reader that uses Kokoro ONNX for chapter-based text-to-speech. It supports background chapter audio preparation and caching, playback position saving, and optional Vosk word timing alignment. See `README.md` for setup and the main source files.

## Durable user requirements

- Keep the reader responsive while audio is generated and played.
- Make chapter audio preparation faster by using available resources effectively, while reporting honest progress and elapsed time.
- Prepare chapters in the background so chapter transitions do not wait for a full-book preload.
- Changing the selected/current chapter must not accidentally cancel or restart valid background preparation for another chapter.
- Preserve playback position and word highlighting through pause, resume, and seek.
- Protect existing audio, timing, progress, and settings files during failures and replacement.
- Work on one improvement at a time, with focused regression checks and periodic realistic smoke tests.
- No UI/UX changes until the separate final UI/UX pass.

## Current technical shape

- `kokoro_novel_reader.py`: desktop application, chapter navigation, playback, caching, and background coordination.
- `kokoro_paragraph_worker.py`: isolated Kokoro chapter rendering worker and structured result protocol.
- `vosk_align_chapter.py`: optional Vosk word alignment.
- `tests/test_reliability_fixes.py`: deterministic regression suite.
- Kokoro model/voice data and Vosk model data are local assets and intentionally excluded from Git.

## Known behavior to keep investigating

- Long chapter rendering can take several minutes; capture per-chapter elapsed time and use it to judge performance changes.
- A failed paragraph has previously caused the chapter output to be discarded. Investigate failure handling and recovery without silently accepting incomplete audio or losing valid cached output.
- Real-model integration is slow and opt-in; deterministic tests alone do not establish real inference speed or quality.

## Baseline checks

Run in PowerShell from the repository root:

```powershell
python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
python -m unittest discover -s .\tests -v
```

The current tracked test suite has included an opt-in real-model integration test that was skipped in ordinary runs. Confirm the current count and skip reason from actual test output; do not rely on old counts.

## Collaboration handoff

The repository shares this context and the concise `AI_WORKLOG.md`, not Codex or Copilot private chat transcripts. Before starting work, read the newest work-log entry. After a task, record what changed, checks actually run, results, open concerns, and a concrete next step. Keep entries free of private conversations, user documents, and novel content.
