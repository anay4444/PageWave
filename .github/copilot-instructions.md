# PageWave instructions for GitHub Copilot

Before making changes, read `AGENTS.md`, `PROJECT_CONTEXT.md`, and the latest entries in `AI_WORKLOG.md`. Treat them as the shared project context and update the work log after each meaningful task so Codex and Copilot can pick up the same decisions and progress.

Follow the repository's one-improvement-at-a-time workflow. Inspect existing code and Git status first, preserve user data and unrelated edits, and run focused checks before reporting results. Do not make UI/UX changes; that work is reserved for a separate final pass. Never add private chat transcripts, personal novel text, model files, generated audio, or local machine data to this public repository.

Use `python -m py_compile .\kokoro_novel_reader.py .\kokoro_paragraph_worker.py .\vosk_align_chapter.py` and `python -m unittest discover -s .\tests -v` as the baseline checks when relevant. The real-model Kokoro integration test is opt-in and may take several minutes.
