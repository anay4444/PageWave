# PageWave agent instructions

Read `PROJECT_CONTEXT.md` and the latest entries in `AI_WORKLOG.md` before starting work. These files are the shared project memory for Codex, GitHub Copilot, and other repository agents.

## How to collaborate

- Keep the shared context current. At the end of each meaningful task, update `AI_WORKLOG.md` with the date, goal, changes, checks and results, remaining risks, and next step. Update `PROJECT_CONTEXT.md` when a lasting requirement, architecture fact, or known issue changes.
- Do not copy private conversation transcripts into this public repository. Record only durable, task-relevant decisions and technical outcomes.
- Before editing, inspect the current implementation and Git status. Preserve unrelated user changes.
- Work on one improvement at a time. Add focused regression coverage and run the relevant checks. Do not claim a check passed unless it was actually run.
- Preserve existing chapter audio, timing, progress, and settings data. Never delete or regenerate user data as cleanup.
- Explain user-visible behavior changes and any tradeoffs in plain language.

## UI/UX final pass

- UI/UX work is reserved for the final project pass. Do not begin it during feature or reliability work.
- When the user starts that pass, use the KING skill router when available to freshly select a small, task-specific specialist skill cast. If KING is unavailable to an agent, follow the shared requirements in `PROJECT_CONTEXT.md` and `AI_WORKLOG.md`.
- Coordinate Codex and GitHub Copilot as complementary contributors when both are available. First inspect their remaining context/work capacity; the agent with less capacity should take a smaller, bounded, independent task, while the agent with more capacity can take the larger design/build or integration task.
- Neither agent can reliably inspect or allocate the other's live token budget unless the product exposes it. Do not invent token counts; ask each contributor to state its available capacity and propose an assignment accordingly.
- Split work by non-overlapping files or components. Agree on design direction and acceptance criteria first, use separate branches or isolated worktrees for parallel changes, then integrate and review one coherent result. Do not have agents overwrite the same files concurrently.
- Share decisions and handoffs through `PROJECT_CONTEXT.md`, `AI_WORKLOG.md`, Git branches, and pull requests. GitHub does not synchronize private chat transcripts between Codex and Copilot.
- The user expects the two contributors to collaborate toward one polished result; include accessibility, responsive behavior, consistency, and regression checks in the acceptance review. Do not report success until the integrated result is checked against the agreed criteria.
- Do not push or publish UI/UX changes until the user asks to start that final pass and the result is ready for their review.

## Project checks

Run from the repository root in PowerShell:

```powershell
python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
python -m unittest discover -s .\tests -v
```

The real Kokoro inference integration test is opt-in and can take several minutes. Do not enable it unless the task specifically needs a real-model smoke test and the local model is available.

## Scope and data

Keep model weights, voice files, Vosk model data, generated audio, personal novels, local analysis artifacts, and machine-specific profiles out of version control. Check `.gitignore` before adding assets.
