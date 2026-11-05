# PageWave instructions for GitHub Copilot

Before making changes, read `AGENTS.md`, `PROJECT_CONTEXT.md`, and the latest entries in `AI_WORKLOG.md`. Treat them as the shared project context and update the work log after each meaningful task so Codex can pick up the same decisions and progress.

Follow the repository's one-improvement-at-a-time workflow. Inspect existing code and Git status first, preserve user data and unrelated edits, and run focused checks before reporting results. Never add private chat transcripts, personal novel text, model files, generated audio, or local machine data to this public repository.

## UI/UX work

UI/UX work is reserved for the final project pass. When the user starts it, use the KING skill router if available to select a small specialist skill cast. Coordinate with Codex using separate, non-overlapping assignments sized to each agent's stated available context: give the more constrained agent a smaller bounded task and the agent with more capacity the broader build or integration task. Do not invent or assume live token counts, and do not edit the same files concurrently. Agree on criteria, use separate branches/worktrees for parallel changes, and integrate and review one coherent result for accessibility, responsive behavior, consistency, and regressions. Keep the handoff in the repository files; private chat transcripts do not sync through GitHub. Wait for the user's direction to start the final UI/UX pass and to publish the finished result.

Use `python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py` and `python -m unittest discover -s .\tests -v` as baseline checks when relevant. The real-model Kokoro integration test is opt-in and may take several minutes.
