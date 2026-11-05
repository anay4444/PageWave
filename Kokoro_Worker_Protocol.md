# Kokoro Worker JSONL Protocol

Protocol version: 1. Each stdout line is one JSON object with `protocol`, `job_id`, and `event`. Standard error is diagnostic text; consumers must not parse it as protocol data.

## Events

- `chapter_start`: `chapter`, `total_chapters`.
- `paragraph`: `chapter`, `paragraph`, `total`, `percent`.
- `paragraph_error`: `chapter`, `paragraph`, `error`.
- `chapter_done`: `chapter`, `status` (`complete`). Emitted only after output files have committed.
- `chapter_error`: `chapter`, `status` (`failed`), `error`, `failed_paragraphs` (one-based paragraph indexes; empty for chapter-level failures).
- `worker_error`: `status` (`failed`), `error` for job-level validation/startup errors.
- `batch_summary`: terminal batch event with `status` (`complete` or `partial_failure`), `total`, and arrays `succeeded` and `failed` containing chapter numbers.

## Cache completeness

The `.complete.json` file uses `format: kokoro-chapter-commit-v1` and `complete: true`, along with `renderer_identity`, WAV size/mtime, and timing-file SHA-256. A cache reader must reject missing/false completeness or renderer identity, pending markers, or mismatched artifact metadata. Reader-generated work passes the cache-key identity through `--cache-identity` so the manifest records the model/runtime/normalization/audio-format inputs.

All stdout events for one process share the same job ID. Readers should reject unknown protocol versions, mismatched job IDs, malformed JSON, and a nonzero process exit even if earlier chapter events reported success. Human-readable diagnostics go to stderr and do not share the JSONL channel.
