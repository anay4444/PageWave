# PageWave shared AI work log

This is the concise, repository-shared handoff for Codex and GitHub Copilot. It records project decisions and work outcomes, not private chat transcripts.

## 2026-11-05 — Shared UI/UX collaboration preference

- User requested that Codex and Copilot collaborate on the best PageWave UI/UX, with work divided by contributor capacity: the more constrained agent gets a smaller independent assignment, and the agent with more capacity gets broader implementation or integration work.
- UI/UX remains reserved for the final project pass; this records the future workflow and does not start redesign work.
- KING skill router should select a small task-specific specialist cast when that final pass begins.
- Parallel work must use separate components/files and branches or worktrees, then be integrated and reviewed as one coherent result. No invented live token counts; contributors report their own available capacity.
- Limitation: GitHub shares repository context and work artifacts, not private Codex/Copilot chat transcripts. Record durable decisions and handoffs here.
- Next: when the user starts the final UI/UX pass, route the task through KING, agree on acceptance criteria, and allocate reviewable assignments.

## 2026-11-05 — Shared Codex/Copilot project context

- Goal: make both coding assistants start from the same PageWave requirements and recent progress.
- Added repository instructions for Codex-compatible agents and GitHub Copilot, plus this project context and shared log.
- Durable requirements captured: improve chapter preparation speed; retain background preparation across chapter navigation; preserve audio/timing/progress/settings data; do not change UI/UX before the user's separate final pass; work one improvement at a time and verify changes.
- Verification: documentation and instruction files created; repository status and the public GitHub repository identity were checked. No application code or tests were changed in this handoff task.
- Limitation: the products' private conversation histories are not automatically synchronized by GitHub. These repository files provide a shared project memory that both assistants can read.

## 2026-11-05 — Final UI/UX pass started

- Goal: polish the desktop reader without publishing UI changes before user review.
- Design direction: retain the dark reading workspace while adding clearer PageWave hierarchy, grouped reading settings, stronger section labels, improved spacing, and responsive pane sizing.
- Accessibility direction: add visible keyboard focus, predictable shortcuts for open/playback/chapter navigation, and bounded text-size controls that do not alter saved reading data.
- Collaboration: visual work was completed in isolated branch `ui-design` by the broader contributor; accessibility work was completed in isolated branch `ui-accessibility` by the constrained contributor. Their commits were reviewed and integrated into `main` without publishing.
- Checks: both contributors passed `py_compile` and `git diff --check`; the integrated code passed `py_compile`, `git diff --check`, and a headless Tk construction/shortcut/text-scale check under Xvfb. The suite ran 58 tests with 55 passing, 2 skipped, and 3 pre-existing failures/errors unrelated to this UI pass: missing `Kokoro_Script_Diagnostics.md`, an optional-module subprocess assertion, and settings-history normalization.
- Remaining risks: visual polish and responsive behavior still need the user's interactive review; no publish action has been taken.

## 2026-11-05 — Preserve pre-identity chapter caches

- User-visible issue: an existing audio folder contained chapter WAVs, but the reader reported 0 saved chapters and started loading again.
- Root cause: cache filenames created before renderer/model identity was appended no longer matched the current identity-based filename, even though their chapter/text/voice/speed inputs were unchanged.
- Fix: `_cache_names` now checks the pre-identity 20-character filename format before the older SHA-1 legacy format. Integrity validation remains unchanged.
- Checks: targeted legacy and pre-identity cache tests passed; Python compilation and `git diff --check` passed.

## 2026-11-05 — Discover older chapter cache filenames

- Follow-up: the first compatibility fix restored only 5 chapters because remaining files used hashes from an older cache format that could not be reconstructed from current chapter text.
- Fix: after exact current/pre-identity/legacy names fail, PageWave now searches the configured novel audio folders for a complete `chapter_NNNN_*.wav` candidate and applies the same WAV/timing/commit validation before reuse. Cache counts use this fallback for the selected voice only, avoiding false attribution to every voice.
- Checks: three cache-reuse tests passed, including an unknown-old-hash case; Python compilation and `git diff --check` passed.

## 2026-11-05 — Reuse moved chapter files safely

- Follow-up: chapters through 98 had WAVs and sidecars but were still skipped during preload.
- Root cause: strict cache validation required the completion manifest's original `audio_mtime_ns`; moving/copying files can change that timestamp even when the WAV bytes remain identical.
- Fix: validation now checks the current WAV size and SHA-256 plus timing SHA-256, while no longer requiring the original mtime. Modified audio is still rejected.
- Checks: moved-cache, unknown-hash discovery, and pre-identity reuse tests passed; compilation and `git diff --check` passed.

## 2026-11-05 — Accept structurally valid legacy sidecars

- Follow-up: the user still saw preload restart at Chapter 83 despite WAV, timing, and completion files through Chapter 98.
- Root cause: legacy discovery reused the same strict completion-manifest validation as current caches, so older sidecars with stale identity/size/hash metadata were rejected.
- Fix: only the unknown-old-hash fallback may now ignore stale completion metadata; it still requires a readable non-empty WAV and valid timing intervals. Current named caches remain strict.
- Checks: stale-sidecar, unknown-hash, and moved-cache tests passed; compilation and `git diff --check` passed.

## 2026-11-05 — Correct legacy fallback call site

- Follow-up: the user still saw Chapter 83 render again. Code review found the fallback was accidentally still calling strict validation; the earlier stale-metadata change had been applied to the exact-name path instead.
- Fix: exact current cache names are strict again; only unknown legacy `chapter_NNNN_*.wav` discovery uses readable-WAV validation and repairs timing metadata when needed.
- Checks: legacy WAV, stale-sidecar, and unknown-hash tests passed; compilation and `git diff --check` passed.

## 2026-11-06 — Prevent Tk GDI exhaustion during playback

- User-visible issue: Windows showed `Tk_GetPixmap: Error from CreateDIBSection` while a long chapter was playing and preload had many chapters ready.
- Root cause: playback called `_highlight` every 150 ms, removing highlight tags across the entire Text widget, toggling its state twice, and forcing full redraws of the long chapter.
- Fix: highlight updates now remove/add only the previous paragraph and word ranges, and playback no longer toggles the Text widget state on every tick.
- Checks: Python compilation, `git diff --check`, and a headless Tk incremental-highlight check passed.

## 2026-11-06 — Bound background chapter preloading

- User-visible issue: long novels continued rendering distant chapters and consumed excessive memory/resources even after many chapters were already ready.
- Root cause: background preload scanned and queued every missing chapter from the active position through the end of the novel.
- Fix: preload now limits new rendering to the current chapter plus three upcoming chapters, while retaining cache reuse and the manual stop control. Status text reports the bounded preparation window.
- Checks: bounded-window and preload-order regression tests passed; Python compilation and `git diff --check` passed. The broader reliability suite still has three unrelated pre-existing failures/errors involving a missing diagnostics file, optional-module subprocess behavior, and settings-history normalization.

## 2026-11-12 — Stop future loading and handle unavailable audio output

- The Windows traceback was a `sounddevice.PortAudioError` from `sd.play`: PortAudio could not open the output stream (`MME error 1`), so the Tk callback raised an uncaught exception.
- The preload control is now labeled “Stop loading future chapters”; it cancels the queue and active worker while preserving completed chapter files.
- Playback now reports the output-device problem in a dialog and returns to the Play state instead of crashing the application.

## 2026-11-12 — Add local phone playback

- Added an opt-in local Wi-Fi phone player. PageWave serves only the currently loaded WAV through a tokenized local URL; no audio is uploaded to Google or another service.
- The “Play on phone” dialog shows and copies the URL. The server stops automatically when PageWave closes, and completed audio/cache files are unaffected.
- Same Google email is not required and does not itself enable Google Cast; Chromecast/Google TV hardware or a Cast receiver is required for native Google Cast.
- Checks: phone HTML/WAV serving, playback error handling, preload stopping, compilation, and `git diff --check` passed.

## 2026-11-12 — Add phone auto-next

- The first phone player served only the currently loaded chapter, so the browser could not honor PageWave's Auto-next setting.
- The phone page now checks the next cached chapter when playback ends, loads it automatically when Auto-next is enabled, and leaves playback stopped when it is disabled or the next chapter is not ready.
- Checks: phone current/next WAV and state endpoint tests, compilation, and `git diff --check` passed.

## 2026-11-12 — Fix repeated phone auto-next

- Follow-up: the phone page advanced once but the server continued reporting the desktop's original chapter, so later transitions could repeat or stop.
- Fix: phone state requests now carry the phone's current chapter and resolve the following cached chapter independently of desktop navigation. Repeated transitions remain governed by the Auto-next setting.
- Checks: repeated phone state/audio transition test, compilation, and `git diff --check` passed.

## 2026-11-12 — Make phone chapter transitions visible

- Follow-up: browser transition failures were swallowed, making phone Auto-next appear inactive.
- Fix: the phone player now initializes its chapter explicitly, exposes a manual “Next chapter” button, reports uncached-next and browser-playback states, and still auto-advances only when Auto-next is enabled.
- Checks: phone transition regression test, compilation, and `git diff --check` passed.

## 2026-11-12 — Align phone cache lookup with desktop

- The phone page reported “Next chapter is not cached yet” while PageWave showed all 266 chapters ready. The phone endpoint was using a narrower voice/speed filename lookup than the desktop cache scanner.
- Fix: phone chapter resolution now tries normalized speed variants and the existing legacy `chapter_NNNN_*.wav` discovery fallback before reporting a chapter unavailable.
- Checks: legacy phone-cache fallback, phone transition, compilation, and `git diff --check` passed.
