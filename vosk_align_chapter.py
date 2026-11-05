"""Create reusable word timestamps for one existing Kokoro chapter WAV.

Usage:
  py -3.12 .\vosk_align_chapter.py "chapter.wav" "C:\\Models\\vosk-model-small-en-us-0.15"

The WAV and paragraph timing JSON are read only. Outputs are written beside the
WAV as `.words.json` and `.recognized.txt`.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import subprocess
import tempfile
import wave
from pathlib import Path

try:
    import audioop
except ImportError:
    audioop = None


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
VOSK_SAMPLE_RATE = 16000
MIN_WORD_CONFIDENCE = 0.5
MAX_ALIGNMENT_TOKENS = 30000
MAX_ALIGNMENT_SECONDS = 60
MIN_PARAGRAPH_EXACT_COVERAGE = 0.5


def normalize(token: str) -> str:
    return re.sub(r"[^\w']", "", token.casefold().replace("’", "'"), flags=re.UNICODE)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def validate_word_order(words: list[dict]) -> list[dict]:
    """Reject word times that move backward anywhere in the full chapter."""
    previous_start = previous_end = -math.inf
    for word in words:
        start, end = float(word["start"]), float(word["end"])
        if start < previous_start or end < previous_end:
            raise ValueError(f"Word timing order moved backward at {word.get('word', 'unknown')!r}")
        previous_start, previous_end = start, end
    return words


def _run_alignment_worker(input_path: Path, output_path: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("paragraphs"), list) or not isinstance(payload.get("recognized"), list):
        raise ValueError("Invalid alignment worker input")
    paragraphs, recognized = payload["paragraphs"], payload["recognized"]
    aligned, totals = align_chapter(paragraphs, recognized)
    output_path.write_text(json.dumps({"paragraphs": aligned, "totals": totals}, allow_nan=False), encoding="utf-8")
    return 0


def align_chapter_bounded(paragraphs: list[dict], recognized: list[dict],
                          timeout: float = MAX_ALIGNMENT_SECONDS) -> tuple[list[dict], dict[str, int]]:
    """Run CPU-heavy sequence matching in a killable process with a firm deadline."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Alignment timeout must be a positive finite number")
    with tempfile.TemporaryDirectory(prefix="kokoro-align-") as folder:
        root = Path(folder)
        input_path, output_path = root / "alignment-input.json", root / "alignment-output.json"
        input_path.write_text(json.dumps({"paragraphs": paragraphs, "recognized": recognized}, allow_nan=False),
                              encoding="utf-8")
        command = [sys.executable, str(Path(__file__).resolve()), "--alignment-worker",
                   str(input_path), str(output_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Alignment exceeded its {timeout:g}-second processing budget. "
                "Split the chapter and try again."
            ) from exc
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout or "Alignment worker failed without details").strip()
            raise RuntimeError(f"Alignment worker failed: {detail[-1000:]}")
        result = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("paragraphs"), list) or not isinstance(result.get("totals"), dict):
            raise RuntimeError("Alignment worker returned malformed output")
        return result["paragraphs"], result["totals"]


def alignment_runtime_metadata(model_path: Path) -> dict:
    try:
        vosk_version = importlib.metadata.version("vosk")
    except importlib.metadata.PackageNotFoundError:
        vosk_version = "unknown"
    model_path = model_path.resolve()
    fingerprint_rows = []
    try:
        for asset in sorted(path for path in model_path.rglob("*") if path.is_file()):
            stat = asset.stat()
            fingerprint_rows.append((asset.relative_to(model_path).as_posix(), stat.st_size, stat.st_mtime_ns))
    except OSError:
        fingerprint_rows = [("manifest-unavailable", 0, 0)]
    model_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "vosk_version": vosk_version,
        "python_version": sys.version.split()[0],
        "model_path": str(model_path.resolve()),
        "model_name": model_path.name,
        "model_metadata_sha256": model_fingerprint,
    }


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text output only after its complete contents are on disk."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", prefix=path.name + ".",
            suffix=".tmp", dir=path.parent, delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_word_timing(result_text: str) -> list[dict]:
    result = json.loads(result_text or "{}")
    words = []
    previous_start = previous_end = -math.inf
    for item in result.get("result", []):
        word = str(item.get("word", "")).strip()
        if word and "start" in item and "end" in item:
            start = float(item["start"])
            end = float(item["end"])
            confidence = float(item.get("conf", 0.0))
            if (not math.isfinite(start) or not math.isfinite(end)
                    or not math.isfinite(confidence) or start < 0 or end <= start
                    or not 0.0 <= confidence <= 1.0):
                raise ValueError(f"Vosk returned invalid timing or confidence for word {word!r}")
            if start < previous_start or end < previous_end:
                raise ValueError(f"Vosk returned out-of-order word timings at {word!r}")
            previous_start, previous_end = start, end
            words.append({
                "word": word,
                "norm": normalize(word),
                "start": start,
                "end": end,
                "confidence": confidence,
            })
    return words


def recognize(wav_path: Path, model_path: Path) -> list[dict]:
    recognized: list[dict] = []
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise RuntimeError("This chapter must be mono, uncompressed, 16-bit PCM WAV.")
        source_rate = wav.getframerate()
        if source_rate != VOSK_SAMPLE_RATE and audioop is None:
            raise RuntimeError(
                "This Python version does not include audioop, which is needed to resample "
                f"{source_rate} Hz audio to {VOSK_SAMPLE_RATE} Hz. On Python 3.13+, install "
                f"audioop-lts with: \"{sys.executable}\" -m pip install audioop-lts; "
                "or run the aligner with Python 3.12."
            )
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise RuntimeError(f"Vosk is missing. Install it with: \"{sys.executable}\" -m pip install vosk") from exc

        SetLogLevel(-1)
        model = Model(str(model_path))
        recognizer = KaldiRecognizer(model, VOSK_SAMPLE_RATE)
        recognizer.SetWords(True)
        total_frames = wav.getnframes()
        read_frames = 0
        resample_state = None
        while True:
            audio = wav.readframes(8000)
            if not audio:
                break
            read_frames += len(audio) // 2
            if source_rate != VOSK_SAMPLE_RATE:
                audio, resample_state = audioop.ratecv(
                    audio, 2, 1, source_rate, VOSK_SAMPLE_RATE, resample_state
                )
            if recognizer.AcceptWaveform(audio):
                recognized.extend(read_word_timing(recognizer.Result()))
            if total_frames:
                percent = int(read_frames * 100 / total_frames)
                print(f"\rRecognizing audio: {percent:3d}%", end="", flush=True)
        recognized.extend(read_word_timing(recognizer.FinalResult()))
    print("\rRecognizing audio: 100%")
    return validate_word_order(recognized)


def align_chapter(paragraphs: list[dict], recognized: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Match the whole chapter in sequence, then restore paragraph grouping.

    Do not gate ASR words by the paragraph timing sidecar: small timing drift at
    a paragraph boundary can otherwise discard good words and ruin all later
    matches. SequenceMatcher keeps long exact runs anchored even when Vosk
    inserts, omits, or mishears a few words.
    """
    validate_word_order(recognized)
    source_paragraphs: list[tuple[str, list[str]]] = []
    source_tokens: list[tuple[int, str]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        text = str(paragraph.get("text", ""))
        tokens = [m.group() for m in WORD_RE.finditer(text)]
        source_paragraphs.append((text, tokens))
        source_tokens.extend((paragraph_index, token) for token in tokens)

    source_norm = [normalize(token) for _, token in source_tokens]
    spoken_norm = [word["norm"] for word in recognized]
    token_budget = len(source_norm) + len(spoken_norm)
    if token_budget > MAX_ALIGNMENT_TOKENS:
        raise RuntimeError(
            f"Chapter is too large for safe whole-chapter alignment ({token_budget:,} tokens; "
            f"limit {MAX_ALIGNMENT_TOKENS:,}). Split the chapter or align smaller sections."
        )
    aligned: list[tuple[dict, str] | None] = [None] * len(source_tokens)
    counts = {"exact": 0, "substitution": 0, "unmatched": 0}

    # Let SequenceMatcher's popular-token heuristic avoid pathological work on
    # repetitive text. The explicit token budget bounds the remaining worst case.
    matcher = difflib.SequenceMatcher(a=source_norm, b=spoken_norm, autojunk=True)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            for ai, bi in zip(range(a0, a1), range(b0, b1)):
                aligned[ai] = (recognized[bi], "exact")
                counts["exact"] += 1
        elif tag == "replace" and (a1 - a0) == (b1 - b0):
            # Same number of source/recognized words: retain the acoustic timing
            # for ASR spelling/name substitutions, and label them for review.
            for ai, bi in zip(range(a0, a1), range(b0, b1)):
                aligned[ai] = (recognized[bi], "substitution")
                counts["substitution"] += 1

    output_paragraphs = [{"paragraph": i, "text": text, "words": []}
                         for i, (text, _) in enumerate(source_paragraphs)]
    paragraph_exact = [0] * len(source_paragraphs)
    paragraph_substitutions = [0] * len(source_paragraphs)
    paragraph_confidences: list[list[float]] = [[] for _ in source_paragraphs]
    for (paragraph_index, token), match in zip(source_tokens, aligned):
        if match is None:
            output_paragraphs[paragraph_index]["words"].append(
                {"text": token, "start": None, "end": None, "match": "unmatched"}
            )
            counts["unmatched"] += 1
        else:
            audio_word, match_type = match
            output_paragraphs[paragraph_index]["words"].append({
                "text": token,
                "start": audio_word["start"],
                "end": audio_word["end"],
                "recognized_as": audio_word["word"],
                "confidence": audio_word["confidence"],
                "match": match_type,
            })
            if match_type == "exact":
                paragraph_exact[paragraph_index] += 1
            else:
                paragraph_substitutions[paragraph_index] += 1
            confidence = float(audio_word.get("confidence", 0.0))
            if math.isfinite(confidence):
                paragraph_confidences[paragraph_index].append(confidence)
    for index, (_, tokens) in enumerate(source_paragraphs):
        coverage = paragraph_exact[index] / len(tokens) if tokens else 0.0
        output_paragraphs[index]["source_word_count"] = len(tokens)
        output_paragraphs[index]["exact_coverage"] = coverage
        output_paragraphs[index]["substitution_count"] = paragraph_substitutions[index]
        confidences = paragraph_confidences[index]
        output_paragraphs[index]["mean_confidence"] = (
            sum(confidences) / len(confidences) if confidences else None
        )
        output_paragraphs[index]["accepted_for_highlighting"] = (
            bool(tokens) and coverage >= MIN_PARAGRAPH_EXACT_COVERAGE
        )
    return output_paragraphs, counts


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--alignment-worker":
        return _run_alignment_worker(Path(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) != 3:
        print('Usage: py -3.12 .\\vosk_align_chapter.py "chapter.wav" "path\\to\\vosk-model-small-en-us-0.15"')
        return 2

    wav_path = Path(sys.argv[1]).expanduser().resolve()
    model_path = Path(sys.argv[2]).expanduser().resolve()
    timing_path = Path(str(wav_path) + ".timing.json")
    if not wav_path.is_file():
        raise RuntimeError(f"WAV not found: {wav_path}")
    if not timing_path.is_file():
        raise RuntimeError(f"Paragraph timing file not found: {timing_path}")
    if not model_path.is_dir():
        raise RuntimeError(f"Vosk model folder not found: {model_path}")

    paragraphs = json.loads(timing_path.read_text(encoding="utf-8"))
    recognized = recognize(wav_path, model_path)
    with wave.open(str(wav_path), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
    if any(word["end"] > duration + 0.05 for word in recognized):
        raise RuntimeError("Vosk returned a word timing beyond the end of the audio")
    output_paragraphs, totals = align_chapter_bounded(paragraphs, recognized)

    stat = wav_path.stat()
    data = {
        "format": "kokoro-vosk-word-timings-v1",
        "audio_file": wav_path.name,
        "audio_size": stat.st_size,
        "audio_mtime_ns": stat.st_mtime_ns,
        "audio_sha256": digest(wav_path),
        "timing_sha256": digest(timing_path),
        "minimum_word_confidence": MIN_WORD_CONFIDENCE,
        "alignment_runtime": alignment_runtime_metadata(model_path),
        "paragraphs": output_paragraphs,
        "totals": totals,
    }
    words_path = Path(str(wav_path) + ".words.json")
    transcript_path = Path(str(wav_path) + ".recognized.txt")
    transcript = "\n".join(
        f"{w['start']:8.3f}\t{w['end']:8.3f}\t{w['word']}\tconfidence={w['confidence']:.2f}"
        for w in recognized
    )
    total = sum(totals.values())
    percent = round(100 * totals["exact"] / max(1, total))
    data["match_percent"] = percent
    data["accepted_for_highlighting"] = percent >= 50
    data["minimum_paragraph_exact_coverage"] = MIN_PARAGRAPH_EXACT_COVERAGE
    # The words file is the reader's consumable result, so publish it last.
    atomic_write_text(transcript_path, transcript)
    atomic_write_text(words_path, json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Source words with audio times: {totals['exact'] + totals['substitution']} / {total} ({percent}%)")
    print(f"Exact text matches: {totals['exact']}; ASR substitutions: {totals['substitution']}; unmatched: {totals['unmatched']}")
    print(f"Saved reusable timings: {words_path}")
    print(f"Saved recognized transcript for review: {transcript_path}")
    if not data["accepted_for_highlighting"]:
        print("WARNING: match rate is below 50%; the reader should not use this sidecar for word highlighting.")
    print("The WAV and original text/timing files were not changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Alignment failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
