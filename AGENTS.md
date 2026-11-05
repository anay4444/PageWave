# PageWave agent instructions

Read `PROJECT_CONTEXT.md` and the latest entries in `AI_WORKLOG.md` before starting work. These files are the shared project memory for Codex, GitHub Copilot, and other repository agents.

## How to collaborate

- Keep the shared context current. At the end of each meaningful task, update `AI_WORKLOG.md` with the date, goal, changes, checks and results, remaining risks, and next step. Update `PROJECT_CONTEXT.md` when a lasting requirement, architecture fact, or known issue changes.
- Do not copy private conversation transcripts into this public repository. Record only durable, task-relevant decisions and technical outcomes.
- Before editing, inspect the current implementation and Git status. Preserve unrelated user changes.
- Work on one improvement at a time. Add focused regression coverage and run the relevant checks. Do not claim a check passed unless it was actually run.
- Preserve existing chapter audio, timing, progress, and settings data. Never delete or regenerate user data as cleanup.
- Do not change UI/UX; the user has reserved that work for a separate final pass.
- Explain user-visible behavior changes and any tradeoffs in plain language.

## Project checks

Run from the repository root in PowerShell:

```powershell
python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
python -m unittest discover -s .\tests -v
```

The real Kokoro inference integration test is opt-in and can take several minutes. Do not enable it unless the task specifically needs a real-model smoke test and the local model is available.

## Scope and data

Keep model weights, voice files, Vosk model data, generated audio, personal novels, local analysis artifacts, and machine-specific profiles out of version control. Check `.gitignore` before adding assets.
