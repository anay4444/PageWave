# PageWave agent instructions

Before work, read PAGEWAVE_COMPLETE_HISTORY.md, PROJECT_CONTEXT.md, and the latest AI_WORKLOG.md entries. PAGEWAVE_COMPLETE_HISTORY.md is the detailed, append-only shared record for Codex, GitHub Copilot, and the project owner.

## How to collaborate

- After each meaningful action, implementation slice, test run, review, or handoff, append a dated entry to PAGEWAVE_COMPLETE_HISTORY.md. Include who acted, goal/reason, exact files edited, what changed, commands/checks and actual outcomes (including skips/failures), remaining concerns, and commit/branch. Record “no files changed” when appropriate.
- Keep AI_WORKLOG.md as a short index of current state and next step. Update PROJECT_CONTEXT.md when lasting requirements, architecture facts, or known issues change.
- The repository is public. Do not add private transcripts, credentials, model data, or new personal novel text. Preserve the source log appendix already included at the user's request.
- Separate verified repository facts from screenshot reports or claims made in another worktree. Never claim a test passed unless it was actually run.
- Before editing, inspect current Git status and implementation. Preserve unrelated user changes and protect existing audio, timing, progress, settings, and caches.
- Work on one improvement at a time. Add focused regression coverage and run relevant checks. Explain user-visible changes and tradeoffs plainly.

## UI/UX final pass

- UI/UX work is reserved for the final project pass.
- When the user starts that pass, use the KING skill router when available to freshly select a small, task-specific specialist skill cast. If KING is unavailable, follow the shared criteria in PAGEWAVE_COMPLETE_HISTORY.md and PROJECT_CONTEXT.md.
- Coordinate Codex and GitHub Copilot as complementary contributors when both are available. Have each state its available context/work capacity; do not invent token counts. Give the more constrained agent a smaller, bounded, independent task and the agent with more capacity the broader build or integration task.
- Split work by non-overlapping files/components and separate branches or worktrees. Integrate and review one coherent result; do not have agents overwrite the same files concurrently.
- GitHub shares repository context and artifacts, not private chat histories. Use the shared history, work log, branches, and pull requests for handoffs.
- Do not publish the finished UI/UX pass until the user has reviewed and asked to publish.

## Project checks

Run from the repository root in PowerShell:

```powershell
python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
python -m unittest discover -s .\tests -v
```

The real Kokoro inference integration test is opt-in and can take several minutes. Do not enable it unless specifically needed and the local model is available.

## Scope and data

Keep model weights, voice files, Vosk model data, generated audio, personal novels, local analysis artifacts, and machine-specific profiles out of version control. Check .gitignore before adding assets.
