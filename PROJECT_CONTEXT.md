# PageWave shared project context

Last reviewed: 2026-11-05

For the detailed chronology, edit history, test outcomes, source work-log appendix, and ongoing agent handoffs, read PAGEWAVE_COMPLETE_HISTORY.md. Read the newest AI_WORKLOG.md entry for a concise status.

## What this project is

PageWave is a Windows desktop reader that uses Kokoro ONNX for chapter-based text-to-speech. It supports background chapter audio preparation and caching, playback position saving, and optional Vosk word timing alignment.

## Durable user requirements

- Keep the reader responsive while audio is generated and played.
- Make chapter audio preparation faster by using available resources, while reporting honest progress and elapsed time.
- Prepare chapters in the background so chapter transitions do not wait for a full-book preload.
- Changing the selected/current chapter must not accidentally cancel or restart valid background preparation for another chapter.
- Preserve playback position and word highlighting through pause, resume, and seek.
- Protect existing audio, timing, progress, and settings files during failures and replacement.
- Work on one improvement at a time, with focused regression checks and periodic realistic smoke tests.
- UI/UX work is reserved for the final pass; do not begin it before the user starts that stage.

## Current technical shape

- kokoro_novel_reader.py: desktop application, chapter navigation, playback, caching, and background coordination.
- kokoro_paragraph_worker.py: isolated Kokoro chapter rendering worker and structured result protocol.
- vosk_align_chapter.py: optional word alignment.
- tests/test_reliability_fixes.py: deterministic regression tests.
- Model weights, voice data, Vosk model data, generated audio, novels, and machine-specific analysis data are local assets and excluded from Git.

## Future UI/UX collaboration

When the user explicitly starts the final UI/UX pass, use KING when available to select a small, relevant design/accessibility specialist cast and define acceptance criteria. Have Codex and Copilot state their available work/context capacity. Give the more constrained contributor a smaller independent component; give the other the broader implementation or integration/review assignment. Use separate branches/worktrees and non-overlapping files, then combine and review one coherent experience. Do not invent token counts. Record decisions and each meaningful contribution in PAGEWAVE_COMPLETE_HISTORY.md and AI_WORKLOG.md. GitHub does not sync private chat transcripts. Wait for the user's review and publish direction before pushing final UI/UX changes.

## Baseline checks

Run from PowerShell at the repository root:
```powershell
python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
python -m unittest discover -s .\tests -v
```

The real Kokoro inference integration test is opt-in and may take several minutes. Confirm test counts and skip reasons from the current output, not old reports.
