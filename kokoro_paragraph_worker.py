import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROTOCOL_VERSION = 1
MIN_SPEED = 0.1
MAX_SPEED = 4.0
VOICE_IDS = {"af_nicole", "af_heart", "af_sarah", "af_bella", "am_adam", "bm_george", "bf_emma"}
MAX_RENDER_DURATION_SECONDS = 6 * 60 * 60
MAX_PARAGRAPH_CHARACTERS = 50000
MIN_AUDIO_SAMPLE_RATE = 8000
MAX_AUDIO_SAMPLE_RATE = 192000


class ChapterRenderError(RuntimeError):
    def __init__(self, message: str, failed_paragraphs: list[int] | None = None):
        super().__init__(message)
        self.failed_paragraphs = failed_paragraphs or []


def emit(event: str, job_id: str, **fields) -> None:
    payload = {"protocol": PROTOCOL_VERSION, "job_id": job_id, "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False), flush=True)


def _has_speakable_characters(text: str) -> bool:
    return any(character.isalnum() for character in text)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_stale_render_temps(output_path: Path, max_age_seconds: float = 7 * 24 * 3600) -> int:
    """Remove only this chapter's abandoned staging files older than a week."""
    output_path = Path(output_path)
    timing_path = Path(str(output_path) + ".timing.json")
    completion_path = Path(str(output_path) + ".complete.json")
    prefixes = (output_path.stem + ".", timing_path.name + ".", completion_path.name + ".")
    suffixes = {".wav", ".tmp"}
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        candidates = list(output_path.parent.iterdir())
    except OSError:
        return 0
    for candidate in candidates:
        if not candidate.is_file() or not candidate.name.startswith(prefixes) or candidate.suffix not in suffixes:
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def render_one(koko, paragraphs, output_wav, voice, speed, chapter_index, job_id,
               cache_identity: str = "direct-render"):
    import numpy as np
    import soundfile as sf

    output_path = Path(output_wav)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clean_stale_render_temps(output_path)
    timing_path = Path(str(output_path) + ".timing.json")
    pending_path = Path(str(output_path) + ".pending")
    completion_path = Path(str(output_path) + ".complete.json")
    with tempfile.NamedTemporaryFile(
        prefix=output_path.stem + ".", suffix=".wav", dir=output_path.parent, delete=False
    ) as stream:
        wav_temp = Path(stream.name)
    wav_writer = None
    timings = []
    failures = []
    position_frames = 0
    rate = None
    try:
        for i, text in enumerate(paragraphs, 1):
            if not text.strip() or not _has_speakable_characters(text):
                emit("paragraph", job_id, chapter=chapter_index, paragraph=i,
                     total=len(paragraphs), percent=round(i * 100 / max(1, len(paragraphs))),
                     skipped=True)
                continue
            try:
                samples, sample_rate = koko.create(text, voice=voice, speed=float(speed), lang="en-us")
            except Exception as exc:
                timings.append({"text": text, "start": position_frames / rate if rate else 0.0,
                                "end": position_frames / rate if rate else 0.0})
                failure = {"paragraph": i, "error": f"{type(exc).__name__}: {exc}"}
                failures.append(failure)
                emit("paragraph_error", job_id, chapter=chapter_index, **failure)
                continue
            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            if samples.size == 0:
                raise ValueError(f"Kokoro returned empty audio for paragraph {i}")
            sample_rate = int(sample_rate)
            if not MIN_AUDIO_SAMPLE_RATE <= sample_rate <= MAX_AUDIO_SAMPLE_RATE:
                raise ValueError(
                    f"Kokoro returned an unsupported sample rate ({sample_rate} Hz); expected "
                    f"{MIN_AUDIO_SAMPLE_RATE}–{MAX_AUDIO_SAMPLE_RATE} Hz"
                )
            if rate is None:
                rate = sample_rate
                wav_writer = sf.SoundFile(wav_temp, mode="w", samplerate=rate, channels=1,
                                          format="WAV", subtype="PCM_16")
            elif sample_rate != rate:
                target_frames = math.ceil(len(samples) * rate / sample_rate)
                pause_frames = int(rate * 0.28)
                if (position_frames + target_frames + pause_frames) / rate > MAX_RENDER_DURATION_SECONDS:
                    raise ValueError(
                        f"Chapter exceeds the {MAX_RENDER_DURATION_SECONDS // 3600}-hour rendering limit; "
                        "split the chapter and try again"
                    )
                if not np.isfinite(samples).all():
                    raise ValueError(f"Kokoro returned non-finite audio samples for paragraph {i}")
                try:
                    from scipy.signal import resample_poly
                except ImportError as exc:
                    raise RuntimeError("Safe sample-rate conversion requires scipy; install requirements.txt") from exc
                divisor = math.gcd(rate, sample_rate)
                samples = resample_poly(samples, rate // divisor, sample_rate // divisor).astype(np.float32, copy=False)
                if samples.size == 0 or not np.isfinite(samples).all():
                    raise ValueError(f"Resampling paragraph {i} produced invalid audio")
            if not np.isfinite(samples).all():
                raise ValueError(f"Kokoro returned non-finite audio samples for paragraph {i}")
            pause_frames = int(rate * 0.28)
            if (position_frames + len(samples) + pause_frames) / rate > MAX_RENDER_DURATION_SECONDS:
                raise ValueError(
                    f"Chapter exceeds the {MAX_RENDER_DURATION_SECONDS // 3600}-hour rendering limit; "
                    "split the chapter and try again"
                )
            start_frame = position_frames
            wav_writer.write(samples)
            position_frames += len(samples)
            timings.append({"text": text, "start": start_frame / rate, "end": position_frames / rate})
            wav_writer.write(np.zeros(pause_frames, dtype=np.float32))
            position_frames += pause_frames
            emit("paragraph", job_id, chapter=chapter_index, paragraph=i,
                 total=len(paragraphs), percent=round(i * 100 / max(1, len(paragraphs))))
        if wav_writer is not None:
            wav_writer.close()
            wav_writer = None
        if failures:
            failed = ", ".join(str(item["paragraph"]) for item in failures)
            raise ChapterRenderError(
                f"Chapter output discarded because {len(failures)} paragraph(s) failed "
                f"(paragraphs: {failed})",
                [int(item["paragraph"]) for item in failures],
            )
        if position_frames <= 0:
            raise ValueError("No readable paragraphs were successfully generated")
    except BaseException:
        if wav_writer is not None:
            wav_writer.close()
        wav_temp.unlink(missing_ok=True)
        raise
    timing_temp = None
    completion_temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=timing_path.name + ".", suffix=".tmp",
            dir=timing_path.parent, delete=False
        ) as stream:
            timing_temp = Path(stream.name)
            json.dump(timings, stream, ensure_ascii=False, allow_nan=False)
        wav_info = sf.info(wav_temp)
        staged_timings = json.loads(timing_temp.read_text(encoding="utf-8"))
        if (wav_info.frames <= 0 or wav_info.samplerate != rate
                or not isinstance(staged_timings, list) or not staged_timings):
            raise RuntimeError("Staged chapter audio or timing data failed validation")
        # The pending marker makes every in-between state an invalid cache hit.
        pending_path.touch(exist_ok=True)
        os.replace(wav_temp, output_path)
        wav_temp = None
        os.replace(timing_temp, timing_path)
        timing_temp = None
        stat = output_path.stat()
        completion = {
            "format": "kokoro-chapter-commit-v1",
            "complete": True,
            "renderer_identity": cache_identity,
            "audio_size": stat.st_size,
            "audio_mtime_ns": stat.st_mtime_ns,
            "audio_sha256": sha256_file(output_path),
            "timing_sha256": sha256_file(timing_path),
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=completion_path.name + ".", suffix=".tmp",
            dir=completion_path.parent, delete=False,
        ) as stream:
            completion_temp = Path(stream.name)
            json.dump(completion, stream, ensure_ascii=False, allow_nan=False)
        os.replace(completion_temp, completion_path)
        completion_temp = None
        pending_path.unlink(missing_ok=True)
    finally:
        if wav_temp is not None:
            wav_temp.unlink(missing_ok=True)
        if timing_temp is not None:
            timing_temp.unlink(missing_ok=True)
        if completion_temp is not None:
            completion_temp.unlink(missing_ok=True)
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render Kokoro chapter audio and paragraph timings.")
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("single", "batch"):
        command = modes.add_parser(mode)
        command.add_argument("--model", required=True, help="Path to kokoro-v1.0.onnx")
        command.add_argument("--voices", required=True, help="Path to voices-v1.0.bin")
        command.add_argument("--voice", required=True, choices=sorted(VOICE_IDS))
        command.add_argument("--speed", required=True, type=float)
        command.add_argument("--cache-identity", default="external-render",
                             help="Renderer/model/cache identity recorded in the completion manifest")
        if mode == "single":
            command.add_argument("--chapter", type=int, default=1,
                                 help="One-based novel chapter number for status and error reporting")
            command.add_argument("--input", required=True, help="JSON file containing paragraph strings")
            command.add_argument("--output", required=True, help="Destination WAV path")
        else:
            command.add_argument("--manifest", required=True, help="JSON list of chapter jobs")
    return parser


def load_paragraphs(path: Path) -> list[str]:
    paragraphs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(paragraphs, list) or any(not isinstance(item, str) for item in paragraphs):
        raise ValueError(f"Expected a JSON list of paragraph strings: {path}")
    paragraphs = [item for item in paragraphs if _has_speakable_characters(item)]
    if not paragraphs:
        raise ValueError(f"No readable paragraphs in input: {path}")
    for index, item in enumerate(paragraphs, 1):
        if len(item) > MAX_PARAGRAPH_CHARACTERS:
            raise ValueError(
                f"Paragraph {index} has {len(item):,} characters; limit is "
                f"{MAX_PARAGRAPH_CHARACTERS:,}. Split the paragraph and try again."
            )
    return paragraphs


def validate_common(args) -> tuple[Path, Path, float]:
    model_path = Path(args.model).expanduser().resolve()
    voices_path = Path(args.voices).expanduser().resolve()
    if not model_path.is_file():
        raise FileNotFoundError(f"Kokoro model file not found: {model_path}")
    if not voices_path.is_file():
        raise FileNotFoundError(f"Kokoro voice file not found: {voices_path}")
    if not math.isfinite(args.speed) or not MIN_SPEED <= args.speed <= MAX_SPEED:
        raise ValueError(f"Speed must be finite and between {MIN_SPEED} and {MAX_SPEED}")
    return model_path, voices_path, args.speed


def main() -> int:
    args = build_parser().parse_args()
    job_id = uuid.uuid4().hex
    try:
        model_path, voices_path, speed = validate_common(args)
        if args.mode == "single":
            paragraphs = load_paragraphs(Path(args.input).expanduser().resolve())
            chapter = args.chapter
            if chapter < 1:
                raise ValueError("Chapter number must be positive")
            emit("chapter_start", job_id, chapter=chapter, total_chapters=1)
            from kokoro_onnx import Kokoro
            koko = Kokoro(str(model_path), str(voices_path))
            try:
                render_one(koko, paragraphs, args.output, args.voice, speed, chapter, job_id,
                           args.cache_identity)
            except Exception as exc:
                emit("chapter_error", job_id, chapter=chapter, status="failed",
                     error=f"{type(exc).__name__}: {exc}",
                     failed_paragraphs=getattr(exc, "failed_paragraphs", []))
                print(f"Chapter {chapter} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                return 1
            emit("chapter_done", job_id, chapter=chapter, status="complete")
            return 0

        manifest_path = Path(args.manifest).expanduser().resolve()
        jobs = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("Batch manifest must be a non-empty JSON list")
        validated_jobs = []
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError("Each batch job must be a JSON object")
            chapter = job.get("chapter")
            output = job.get("output")
            paragraphs = job.get("paragraphs")
            if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
                raise ValueError("Each batch job needs a positive integer chapter number")
            if not isinstance(output, str) or not output.strip():
                raise ValueError(f"Chapter {chapter}: output path is missing")
            if not isinstance(paragraphs, list) or any(not isinstance(item, str) for item in paragraphs):
                raise ValueError(f"Chapter {chapter}: paragraphs must be a list of strings")
            paragraphs = [item for item in paragraphs if _has_speakable_characters(item)]
            if not paragraphs:
                raise ValueError(f"Chapter {chapter}: no readable paragraphs")
            for paragraph_index, text in enumerate(paragraphs, 1):
                if len(text) > MAX_PARAGRAPH_CHARACTERS:
                    raise ValueError(
                        f"Chapter {chapter}, paragraph {paragraph_index} exceeds the "
                        f"{MAX_PARAGRAPH_CHARACTERS:,}-character limit"
                    )
            validated_jobs.append((chapter, paragraphs, Path(output)))
        from kokoro_onnx import Kokoro
        koko = Kokoro(str(model_path), str(voices_path))
        all_succeeded = True
        succeeded_chapters = []
        failed_chapters = []
        for chapter, paragraphs, output in validated_jobs:
            emit("chapter_start", job_id, chapter=chapter, total_chapters=len(validated_jobs))
            try:
                render_one(koko, paragraphs, output, args.voice, speed, chapter, job_id,
                           args.cache_identity)
                emit("chapter_done", job_id, chapter=chapter, status="complete")
                succeeded_chapters.append(chapter)
            except Exception as exc:
                all_succeeded = False
                failed_chapters.append(chapter)
                emit("chapter_error", job_id, chapter=chapter, status="failed",
                     error=f"{type(exc).__name__}: {exc}",
                     failed_paragraphs=getattr(exc, "failed_paragraphs", []))
                print(f"Chapter {chapter} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        emit("batch_summary", job_id, status="complete" if all_succeeded else "partial_failure",
             total=len(validated_jobs), succeeded=succeeded_chapters, failed=failed_chapters)
        return 0 if all_succeeded else 1
    except Exception as exc:
        emit("worker_error", job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        print(f"Worker failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
