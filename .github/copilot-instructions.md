# PageWave instructions for GitHub Copilot

Before working, read PAGEWAVE_COMPLETE_HISTORY.md, AGENTS.md, PROJECT_CONTEXT.md, and the latest AI_WORKLOG.md entries. PAGEWAVE_COMPLETE_HISTORY.md is the detailed shared project record used by Copilot and Codex.

After each meaningful action, code change, test run, review, or handoff, append a dated entry to PAGEWAVE_COMPLETE_HISTORY.md. Say who acted, why, which exact files changed, what was done, checks actually run and their results, remaining concerns, and branch/commit. Update AI_WORKLOG.md with a concise current status/next step; update PROJECT_CONTEXT.md for lasting requirements or technical facts. Never claim unrun checks passed.

Keep UI/UX work for the user's final pass. When the user starts it, use the KING router if available, coordinate with Codex on separate components/files and branches, and size assignments to each agent's stated available context. Give the more constrained contributor a smaller bounded task; do not fabricate live token counts or edit the same files concurrently. Integrate and review one coherent result for accessibility, responsive behavior, consistency, and regressions. Do not publish before user review and direction.

The repository is public. Do not add private chat transcripts, credentials, model files, generated audio, or new personal novel text. GitHub shares project files and handoffs, not the assistants' private conversation histories.

Baseline checks when relevant:
- python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py
- python -m unittest discover -s .\tests -v

The real-model Kokoro integration is opt-in and may take several minutes.
