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
