from __future__ import annotations

import hashlib
import bisect
import ctypes
import html
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

OPTIONAL_IMPORT_ERRORS = []
try:
    import sounddevice as sd
except (ImportError, OSError) as exc:
    sd = None
    OPTIONAL_IMPORT_ERRORS.append(f"sounddevice (audio playback): {exc}")
try:
    import soundfile as sf
except (ImportError, OSError) as exc:
    sf = None
    OPTIONAL_IMPORT_ERRORS.append(f"soundfile (audio loading/cache checks): {exc}")
try:
    import pymupdf as fitz
except (ImportError, OSError) as exc:
    fitz = None
    OPTIONAL_IMPORT_ERRORS.append(f"PyMuPDF (PDF opening): {exc}")

for _module_name, _feature_name in (("kokoro_onnx", "Kokoro speech generation"),
                                   ("vosk", "word-timing alignment")):
    try:
        if importlib.util.find_spec(_module_name) is None:
            OPTIONAL_IMPORT_ERRORS.append(f"{_module_name} ({_feature_name}) is not installed")
    except (ImportError, ValueError, ModuleNotFoundError) as exc:
        OPTIONAL_IMPORT_ERRORS.append(f"{_module_name} ({_feature_name}) is unavailable: {exc}")

APP_TITLE = "PageWave"
RENDER_CACHE_VERSION = "kokoro-render-v4"
TEXT_NORMALIZATION_VERSION = "speech-text-v1"
RENDER_AUDIO_FORMAT = "WAV/PCM_16"
MIN_WORD_HIGHLIGHT_CONFIDENCE = 0.5
SETTINGS_SCHEMA_VERSION = 1
MIN_SPEED = 0.1
MAX_SPEED = 4.0
AUDIO_DECODE_CHUNK_FRAMES = 262_144
AUDIO_IN_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
KOKORO_WORKER_PRIORITY = getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0)
PRELOAD_LOOKAHEAD_CHAPTERS = 3
PHONE_CAST_PORT = 8765
PHONE_KEEP_AWAKE_FLAGS = 0x80000000 | 0x00000001
PHONE_SCREEN_OFF_DELAY_MS = 5 * 60 * 1000
PHONE_SCREEN_OFF_SHORTCUT_NAME = "Turn_Off_Screen_without_lock.lnk"

VOICE_OPTIONS = {
    "Nicole (US female, soft)": "af_nicole",
    "Heart (US female)": "af_heart",
    "Sarah (US female)": "af_sarah",
    "Bella (US female)": "af_bella",
    "Adam (US male)": "am_adam",
    "George (UK male)": "bm_george",
    "Emma (UK female)": "bf_emma",
}


def speech_text(text: str) -> str:
    """Keep readable words and normal sentence punctuation; omit decorative symbols."""
    allowed_punctuation = set(".,!?;:'\"-–—()")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("…", "...")
    cleaned = []
    for char in text:
        if char.isalnum() or char.isspace() or char in allowed_punctuation:
            cleaned.append(char)
        else:
            cleaned.append(" ")
    normalized = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    return normalized if any(char.isalnum() for char in normalized) else ""


def _preload_order(missing_indices: list[int], start_index: int) -> list[int]:
    """Prepare the selected chapter and forward chapters before filling earlier gaps."""
    return ([index for index in missing_indices if index >= start_index]
            + [index for index in missing_indices if index < start_index])


def _preload_window(total_chapters: int, start_index: int,
                    lookahead: int = PRELOAD_LOOKAHEAD_CHAPTERS) -> range:
    """Bound background rendering to the current chapter and nearby chapters."""
    first = max(0, start_index)
    last = min(total_chapters, first + max(1, lookahead + 1))
    return range(first, last)


class PhoneAudioServer:
    """Serve loaded chapter audio and cached auto-next chapters locally."""

    def __init__(self, audio_path_getter, title_getter, state_getter=None):
        self._audio_path_getter = audio_path_getter
        self._title_getter = title_getter
        self._state_getter = state_getter
        self._token = secrets.token_urlsafe(18)
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        if self._server is None:
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    if query.get("token", [None])[0] != owner._token:
                        self.send_error(403, "Invalid phone link")
                        return
                    if urllib.parse.urlsplit(self.path).path == "/":
                        owner._serve_page(self)
                    elif urllib.parse.urlsplit(self.path).path == "/state":
                        owner._serve_state(self, query)
                    elif urllib.parse.urlsplit(self.path).path == "/audio":
                        owner._serve_audio(self, query)
                    else:
                        self.send_error(404)

                def log_message(self, _format, *_args):
                    return

            try:
                self._server = ThreadingHTTPServer(("0.0.0.0", PHONE_CAST_PORT), Handler)
                self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
                self._thread.start()
                _set_phone_keep_awake(True)
            except Exception:
                self.stop()
                raise
        return self.url()

    def url(self) -> str:
        host = _local_network_address()
        return f"http://{host}:{PHONE_CAST_PORT}/?token={urllib.parse.quote(self._token)}"

    def stop(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        _set_phone_keep_awake(False)

    def _serve_page(self, handler):
        title = html.escape(str(self._title_getter() or "PageWave"))
        initial_state = self._state_getter() if self._state_getter else {}
        initial_chapter = initial_state.get("chapter")
        chapter_query = (
            f"&chapter={int(initial_chapter) + 1}"
            if initial_chapter is not None else ""
        )
        audio_url = html.escape(
            f"/audio?token={urllib.parse.quote(self._token)}{chapter_query}", quote=True
        )
        state_url = html.escape(f"/state?token={urllib.parse.quote(self._token)}", quote=True)
        body = f"""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - PageWave</title>
<style>body{{font:16px system-ui;background:#101827;color:#e6edf7;padding:24px}}
 h1{{font-size:1.2rem}}audio{{width:100%;margin-top:20px}}
button{{font:inherit;padding:8px 14px;margin-top:12px}}</style>
<h1 id="title">{title}</h1><p>Playing from PageWave on this computer.</p>
<audio id="audio" controls autoplay preload="auto" src="{audio_url}"></audio>
<div><button id="next" type="button">Next chapter</button>
<span id="status" aria-live="polite">Auto-next: ON</span></div>
<script>
const player = document.getElementById("audio");
const title = document.getElementById("title");
const status = document.getElementById("status");
const nextButton = document.getElementById("next");
const stateUrl = "{state_url}";
let currentChapter = {int(initial_chapter) + 1 if initial_chapter is not None else "null"};
async function loadNextChapter(automatic) {{
  try {{
    const chapterQuery = currentChapter === null ? "" : "&chapter=" + currentChapter;
    let state = null;
    for (let attempt = 0; attempt < (automatic ? 30 : 1); attempt++) {{
      state = await fetch(stateUrl + chapterQuery + "&_=" + Date.now()).then(response => response.json());
      if (state.next_available) break;
      if (!automatic) {{
        status.textContent = "Next chapter is not cached yet.";
        return;
      }}
      status.textContent = "Waiting for the next chapter to finish loading…";
      await new Promise(resolve => setTimeout(resolve, 2000));
    }}
    if (!state || !state.next_available) {{
      status.textContent = "Next chapter is still loading. Tap Next chapter when it is ready.";
      return;
    }}
    if (automatic && !state.auto_next) {{
      status.textContent = "Auto-next is off.";
      return;
    }}
    player.src = state.next_url;
    title.textContent = state.next_title;
    currentChapter = state.next_chapter;
    status.textContent = "Loading " + state.next_title + "…";
    await new Promise((resolve, reject) => {{
      const ready = () => {{ cleanup(); resolve(); }};
      const failed = () => {{ cleanup(); reject(new Error("Audio could not load")); }};
      const cleanup = () => {{
        player.removeEventListener("canplay", ready);
        player.removeEventListener("error", failed);
      }};
      player.addEventListener("canplay", ready, {{ once: true }});
      player.addEventListener("error", failed, {{ once: true }});
      player.load();
    }});
    await player.play();
    status.textContent = "";
  }} catch (_error) {{
    status.textContent = "Next chapter loaded; tap Play if your browser blocked automatic playback.";
  }}
}}
nextButton.addEventListener("click", () => loadNextChapter(false));
player.addEventListener("ended", () => loadNextChapter(true));
</script>
"""
        encoded = body.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    def _serve_state(self, handler, query):
        requested_chapter = query.get("chapter", [None])[0]
        try:
            chapter_index = int(requested_chapter) - 1 if requested_chapter is not None else None
        except ValueError:
            handler.send_error(400, "Invalid chapter")
            return
        state = self._state_getter(chapter_index) if self._state_getter else {
            "auto_next": False, "next_available": False
        }
        chapter = state.get("chapter")
        next_path = state.get("next_path")
        payload = {
            "auto_next": bool(state.get("auto_next")),
            "next_available": bool(next_path and next_path.is_file()),
            "next_title": str(state.get("next_title") or ""),
            "next_chapter": int(chapter) + 2 if next_path and next_path.is_file() and chapter is not None else None,
            "next_url": (
                f"/audio?token={urllib.parse.quote(self._token)}&chapter={int(chapter) + 2}"
                if next_path and next_path.is_file() and chapter is not None else ""
            ),
        }
        encoded = json.dumps(payload).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    def _serve_audio(self, handler, query):
        path = self._audio_path_getter()
        if self._state_getter and query.get("chapter", [None])[0] is not None:
            try:
                requested = int(query["chapter"][0]) - 1
            except ValueError:
                handler.send_error(400, "Invalid chapter")
                return
            state = self._state_getter()
            if requested == state.get("chapter"):
                path = state.get("audio_path")
            elif requested == state.get("chapter", -1) + 1:
                path = state.get("next_path")
            else:
                path = None
        if path is None or not path.is_file():
            handler.send_error(404, "No chapter audio is loaded")
            return
        try:
            size = path.stat().st_size
            with path.open("rb") as audio_file:
                handler.send_response(200)
                handler.send_header("Content-Type", "audio/wav")
                handler.send_header("Content-Length", str(size))
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()
                shutil.copyfileobj(audio_file, handler.wfile)
        except (OSError, BrokenPipeError):
            return


def _local_network_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _set_phone_keep_awake(enabled: bool) -> None:
    """Prevent Windows system sleep while the local phone player is serving audio."""
    if os.name != "nt":
        return
    try:
        flags = PHONE_KEEP_AWAKE_FLAGS if enabled else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        return


def _format_elapsed(seconds: float) -> str:
    """Format a monotonic duration for the preload status and timing log."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _append_preload_timing(path: Path, chapter: int, elapsed_seconds: float,
                           result: str, voice: str, speed: str) -> None:
    """Append one completed worker timing without rewriting prior measurements."""
    record = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chapter": int(chapter),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "result": result,
        "voice": voice,
        "speed": speed,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def split_novel(text: str) -> list[tuple[str, str]]:
    # WN Scraper separates chapters with a line of 50 equals signs.
    blocks = re.split(r"\n\s*={10,}\s*\n", text)
    chapters: list[tuple[str, str]] = []
    for i, block in enumerate(blocks, 1):
        block = block.strip().replace("\r\n", "\n")
        if not block:
            continue
        lines = block.splitlines()
        title = lines[0].strip() if lines else f"Chapter {i}"
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else block
        if not body:
            body = block
        chapters.append((title, body))
    if len(chapters) <= 1:
        # Accept ordinary TXT novels with chapter headings and no scraper separators.
        parts = re.split(r"(?im)(?=^\s*(?:chapter|prologue|epilogue)\s+[^\n]+$)", text.strip())
        if len(parts) > 1:
            chapters = []
            for i, part in enumerate(parts, 1):
                part = part.strip()
                if not part:
                    continue
                first, _, rest = part.partition("\n")
                chapters.append((first.strip() or f"Chapter {i}", rest.strip() or part))
    return chapters or [("Novel", text.strip())]


def _bounded_float(value, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        return None
    return parsed


def _directory_write_probe(path: Path) -> bool:
    """Verify write access by creating and removing a uniquely named temp file."""
    probe_path = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".kokoro-write-check-", dir=path)
        os.close(descriptor)
        probe_path = Path(name)
        probe_path.unlink()
        return True
    except OSError:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _directory_status(path: Path, allow_missing: bool = False, require_write: bool = True) -> str | None:
    """Return a short actionable issue for a configured directory path."""
    path = path.expanduser()
    if path.exists():
        if not path.is_dir():
            return "is a file, not a folder"
        try:
            with os.scandir(path) as entries:
                next(entries, None)
        except OSError:
            return "is not readable"
        if require_write and not _directory_write_probe(path):
            return "is not writable"
        return None
    if not allow_missing:
        return "does not exist"
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or (require_write and not _directory_write_probe(parent)):
        return "cannot be created under the current parent folder"
    return None


def _read_audio_for_playback(path: Path):
    if sf is None:
        raise RuntimeError("Audio loading is unavailable because soundfile could not be imported.")
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Chunked audio loading requires NumPy in the reader's Python environment.") from exc

    info = sf.info(path)
    frames, channels = int(info.frames), int(info.channels)
    if frames <= 0 or channels <= 0 or int(info.samplerate) <= 0:
        raise RuntimeError("This audio file has no playable samples or an invalid format.")
    required_bytes = frames * channels * np.dtype(np.float32).itemsize
    if required_bytes <= AUDIO_IN_MEMORY_LIMIT_BYTES:
        audio, samplerate = sf.read(path, dtype="float32", always_2d=True)
        if len(audio) != frames:
            raise RuntimeError("Audio file ended before the declared frame count while loading.")
        return audio, int(samplerate), _sha256_path(path)

    temp_dir = Path(tempfile.gettempdir())
    available_bytes = shutil.disk_usage(temp_dir).free
    if required_bytes > available_bytes:
        raise RuntimeError(
            f"Chunked playback needs about {required_bytes / (1024 ** 3):.1f} GiB of temporary disk space, "
            f"but only {available_bytes / (1024 ** 3):.1f} GiB is available. Free disk space and try again."
        )

    descriptor, mapped_path = tempfile.mkstemp(prefix="kokoro-playback-", suffix=".f32", dir=temp_dir)
    os.close(descriptor)
    audio = None
    try:
        audio = np.memmap(mapped_path, dtype=np.float32, mode="w+", shape=(frames, channels))
        with sf.SoundFile(path, mode="r") as source:
            cursor = 0
            while cursor < frames:
                count = min(AUDIO_DECODE_CHUNK_FRAMES, frames - cursor)
                chunk = source.read(frames=count, dtype="float32", always_2d=True)
                if len(chunk) != count:
                    raise RuntimeError("Audio file ended before the declared frame count while loading.")
                audio[cursor:cursor + count] = chunk
                cursor += count
        audio.flush()
        return audio, int(info.samplerate), _sha256_path(path)
    except Exception:
        if audio is not None:
            audio._mmap.close()
        Path(mapped_path).unlink(missing_ok=True)
        raise


def _release_audio_buffer(audio) -> None:
    """Close and remove the temporary memory map used for chunked playback."""
    if audio is None:
        return
    mapped_file = getattr(audio, "filename", None)
    mapped_handle = getattr(audio, "_mmap", None)
    if mapped_handle is not None:
        try:
            mapped_handle.close()
        except (OSError, ValueError):
            pass
    if mapped_file:
        try:
            Path(mapped_file).unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_path(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _cache_audio_digest_matches(audio_path: Path, audio_hash: str) -> bool:
    completion_path = Path(str(audio_path) + ".complete.json")
    if not completion_path.is_file():
        return True  # Legacy audio has no commit manifest.
    try:
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        return (isinstance(completion, dict)
                and completion.get("audio_sha256") == audio_hash
                and completion.get("complete") is True)
    except (OSError, ValueError, TypeError):
        return False


def _sanitize_settings(raw_settings, settings_needs_recovery: bool = False) -> tuple[dict, bool]:
    recovered = settings_needs_recovery
    if not isinstance(raw_settings, dict):
        raw_settings = {}
        recovered = True
    settings = {"schema_version": SETTINGS_SCHEMA_VERSION, "novels": {}}
    for key in ("output_root", "model_dir"):
        value = raw_settings.get(key)
        if isinstance(value, str) and value.strip() and "\0" not in value:
            settings[key] = value
        elif value is not None:
            recovered = True

    raw_novels = raw_settings.get("novels", {})
    if isinstance(raw_novels, dict):
        for novel_key, raw_record in raw_novels.items():
            if not isinstance(novel_key, str) or not isinstance(raw_record, dict):
                recovered = True
                continue
            record = {}
            chapter_idx = raw_record.get("chapter_idx", 0)
            if isinstance(chapter_idx, int) and not isinstance(chapter_idx, bool) and chapter_idx >= 0:
                record["chapter_idx"] = chapter_idx
            else:
                record["chapter_idx"] = 0
                recovered = True
            offset = _bounded_float(raw_record.get("offset", 0.0), 0.0, 1e12)
            record["offset"] = offset if offset is not None else 0.0
            if offset is None:
                recovered = True
            voice = raw_record.get("voice")
            if voice in VOICE_OPTIONS.values():
                record["voice"] = voice
            elif voice is not None:
                recovered = True
            speed = _bounded_float(raw_record.get("speed", 1.0), MIN_SPEED, MAX_SPEED)
            record["speed"] = speed if speed is not None else 1.0
            if speed is None:
                recovered = True
            audio_folder = raw_record.get("audio_folder", "")
            if isinstance(audio_folder, str) and "\0" not in audio_folder:
                record["audio_folder"] = audio_folder
            elif audio_folder is not None:
                record["audio_folder"] = ""
                recovered = True
            else:
                record["audio_folder"] = ""
            history = raw_record.get("audio_folder_history", [])
            if isinstance(history, list):
                valid_history = []
                seen_history = set()
                for item in history:
                    if not isinstance(item, str) or not item.strip() or "\0" in item:
                        recovered = True
                        continue
                    normalized = os.path.normcase(os.path.abspath(os.path.expanduser(item)))
                    if normalized not in seen_history:
                        seen_history.add(normalized)
                        valid_history.append(item)
                    else:
                        recovered = True
                record["audio_folder_history"] = valid_history
                if len(valid_history) != len(history):
                    recovered = True
            else:
                record["audio_folder_history"] = []
                recovered = True
            settings["novels"][novel_key] = record
    else:
        recovered = True
    if raw_settings.get("schema_version") not in (None, SETTINGS_SCHEMA_VERSION):
        recovered = True
    return settings, recovered


def _drain_text_stream(stream, destination: list[str]) -> None:
    if stream is None:
        return
    for line in stream:
        destination.append(line.rstrip())


def _terminate_process_bounded(process, timeout: float = 2.0) -> bool:
    """Terminate a child and escalate to kill if it does not exit promptly."""
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return process.poll() is not None
    return process.poll() is not None


def _valid_batch_summary(event: dict, expected: set[int], successful: set[int]) -> bool:
    succeeded = event.get("succeeded")
    failed = event.get("failed")
    if (not isinstance(succeeded, list) or not isinstance(failed, list)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in succeeded + failed)):
        return False
    succeeded_set, failed_set = set(succeeded), set(failed)
    expected_status = "complete" if not failed_set else "partial_failure"
    return (
        len(succeeded_set) == len(succeeded)
        and len(failed_set) == len(failed)
        and not succeeded_set & failed_set
        and succeeded_set | failed_set == expected
        and succeeded_set == successful
        and event.get("total") == len(expected)
        and event.get("status") == expected_status
    )


class NovelReader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1050x720")
        self.root.minsize(760, 500)
        self.novel_path: Path | None = None
        self.audio_folder: Path | None = None
        self.resume_offset = 0.0
        appdata = Path(os.environ.get("APPDATA", str(Path.home())))
        self.settings_path = appdata / "KokoroNovelReader" / "settings.json"
        settings_parse_failed = False
        try:
            raw_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_settings = {}
            settings_parse_failed = self.settings_path.exists()
        if settings_parse_failed:
            self._backup_corrupt_settings(self.settings_path)
        self.settings, settings_recovered = _sanitize_settings(raw_settings, settings_parse_failed)
        self.settings_recovered = settings_recovered
        self.settings_recovery_details = []
        self.output_root = Path(self.settings.get("output_root") or (Path.home() / "Documents" / "Kokoro Audio"))
        saved_model_dir = self.settings.get("model_dir")
        self.model_dir: Path | None = Path(saved_model_dir) if saved_model_dir else None
        self._renderer_content_fingerprints: dict[str, tuple[int, int, str]] = {}
        output_issue = _directory_status(self.output_root, allow_missing=True)
        if output_issue:
            self.settings_recovery_details.append(f"Audio output folder {output_issue}: {self.output_root}")
            self.output_root = Path.home() / "Documents" / "Kokoro Audio"
            self.settings_recovered = True
        if self.model_dir is not None:
            model_issue = _directory_status(self.model_dir, require_write=False)
            if model_issue:
                self.settings_recovery_details.append(f"Kokoro model folder {model_issue}: {self.model_dir}")
                self.model_dir = None
                self.settings_recovered = True
        self.model_status: ttk.Label | None = None
        self.chapters: list[tuple[str, str]] = []
        self.chapter_idx = 0
        self.audio = None
        self.current_audio_path: Path | None = None
        self.phone_audio_speed = "1.0"
        self.phone_auto_next_enabled = True
        self.phone_audio_server = PhoneAudioServer(
            lambda: self.current_audio_path,
            lambda: f"Chapter {self.chapter_idx + 1} of {len(self.chapters)}",
            self._phone_audio_state,
        )
        self.phone_screen_off_after = None
        self.phone_screen_off_shortcut: Path | None = None
        self.loaded_audio_voice_id: str | None = None
        self.samplerate = 0
        self._audio_load_generation = 0
        self.duration = 0.0
        self.offset = 0.0
        self.started_at: float | None = None
        self.playing = False
        self.lines: list[str] = []
        self.timing_line_map: list[int] = []
        self.paragraph_timings: list[dict] = []
        self._valid_timing_indices: list[int] = []
        self._valid_timing_starts: list[float] = []
        self.word_spans: list[list[dict]] = []
        self.line_starts: list[float] = []
        self._highlight_range: tuple[str, str] | None = None
        self._highlight_word_range: tuple[str, str] | None = None
        self.chapter_list_labels: list[str] = []
        self.busy = False
        self.prefetching: set[int] = set()
        self.prefetch_bar: ttk.Progressbar | None = None
        self.prefetch_info: ttk.Label | None = None
        self.prefetch_ready: set[int] = set()
        self.prefetch_voice_id: str | None = None
        self.prefetch_speed: str | None = None
        self.audio_cache_counts: dict[str, int] = {}
        self.audio_cache_chapters: dict[str, set[int]] = {}
        self.audio_cache_speed: str | None = None
        self.cache_scan_generation = 0
        self.cache_scan_after = None
        self.preloading = False
        self.preload_state = "idle"
        self._closing = False
        self._background_threads: set[threading.Thread] = set()
        self._background_threads_lock = threading.Lock()
        self.preload_process: subprocess.Popen | None = None
        self.generation_process: subprocess.Popen | None = None
        self.preload_cancel_event: threading.Event | None = None
        self.preload_restart_requested = False
        self.preload_active_started_at: float | None = None
        self.preload_last_timing_text = ""
        self.generation_window: tk.Toplevel | None = None
        self.generation_progress: ttk.Progressbar | None = None
        self.generation_detail: ttk.Label | None = None
        self.generation_percent: ttk.Label | None = None
        self.generation_started = 0.0
        self.text_font_family = "Segoe UI"
        self.text_font_size = 13
        self._build_ui()
        if OPTIONAL_IMPORT_ERRORS:
            issue_text = "Some features have missing dependencies:\n\n" + "\n".join(OPTIONAL_IMPORT_ERRORS)
            self.status.configure(text="Some features are unavailable; see setup details")
            self._schedule_after(100, lambda details=issue_text: messagebox.showwarning("Kokoro setup issue", details))
        if self.settings_recovered:
            details = "; ".join(self.settings_recovery_details)
            self.status.configure(text=("Some saved settings were invalid; safe defaults were loaded" +
                                        (f" ({details})" if details else "")))
        if self.model_dir and self.model_dir.is_dir() and (self.model_dir / "kokoro-v1.0.onnx").is_file() and (self.model_dir / "voices-v1.0.bin").is_file() and importlib.util.find_spec("kokoro_onnx") is not None:
            self.model_status.configure(text="✓ Saved Kokoro folder verified", foreground="#16733a")
        self._schedule_after(150, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        colors = {
            "window": "#101827",
            "panel": "#162235",
            "panel_alt": "#1b2b42",
            "text": "#e6edf7",
            "muted": "#9fb2ca",
            "accent": "#72ddc0",
            "accent_dark": "#276c69",
            "button": "#26364d",
            "button_active": "#365174",
            "editor": "#111b2a",
        }
        self.root.configure(bg=colors["window"])
        style.configure("TFrame", background=colors["window"])
        style.configure("Card.TFrame", background=colors["panel"])
        style.configure("TLabel", background=colors["window"], foreground=colors["text"], font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=colors["window"], foreground=colors["text"], font=("Segoe UI", 17, "bold"))
        style.configure("Subtitle.TLabel", background=colors["window"], foreground=colors["muted"], font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=colors["window"], foreground=colors["accent"], font=("Segoe UI", 9, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6), background=colors["button"],
                        foreground="#f4f7fb", focuscolor="#58d6a3")
        style.map("TButton", background=[("active", colors["button_active"]), ("disabled", "#202b3a")])
        style.configure("TCheckbutton", background=colors["window"], foreground=colors["text"])
        style.configure("TCombobox", fieldbackground="#1c293b", background=colors["button"], foreground="#f4f7fb")
        style.configure("TLabelframe", background=colors["window"], foreground=colors["muted"])
        style.configure("TLabelframe.Label", background=colors["window"], foreground=colors["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("Horizontal.TProgressbar", troughcolor=colors["button"], background="#58d6a3", thickness=12)

        top = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="PageWave", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.file_label = ttk.Label(top, text="Open a TXT file to begin", style="Subtitle.TLabel", anchor="w")
        self.file_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        actions = ttk.Frame(top)
        actions.grid(row=0, column=1, rowspan=1, sticky="e")
        self.open_btn = ttk.Button(actions, text="Open novel", command=self.open_novel, underline=0)
        self.open_btn.pack(side="left")
        ttk.Button(actions, text="Kokoro folder…", command=self.choose_model_dir).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Audio location…", command=self.choose_audio_folder).pack(side="left", padx=(8, 0))

        controls = ttk.LabelFrame(self.root, text="Reading settings", padding=(10, 7))
        controls.pack(fill="x", padx=14, pady=(0, 4))
        controls.columnconfigure(5, weight=1)
        ttk.Label(controls, text="Voice", takefocus=False).grid(row=0, column=0, sticky="w")
        self.voice_label = tk.StringVar(value="Nicole (US female, soft)")
        voice_box = ttk.Combobox(controls, textvariable=self.voice_label, values=list(VOICE_OPTIONS), state="readonly", width=27)
        voice_box.grid(row=0, column=1, sticky="w", padx=(6, 14))
        voice_box.bind("<<ComboboxSelected>>", self._voice_changed)
        ttk.Label(controls, text="Speed", takefocus=False).grid(row=0, column=2, sticky="w")
        self.speed = tk.StringVar(value="1.0")
        speed_entry = ttk.Entry(controls, textvariable=self.speed, width=6)
        speed_entry.grid(row=0, column=3, sticky="w", padx=(6, 14))
        self.auto_next = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Auto-next chapter", variable=self.auto_next).grid(row=0, column=4, sticky="w", padx=(0, 14))
        self.auto_next.trace_add(
            "write", lambda *_: setattr(self, "phone_auto_next_enabled", bool(self.auto_next.get()))
        )
        self.model_status = ttk.Label(controls, text="Kokoro folder not checked", foreground="#d8ae5b", anchor="e")
        self.model_status.grid(row=0, column=5, sticky="e")
        self.voice_status = ttk.Label(self.root, text="", padding=(16, 1, 16, 7), foreground=colors["muted"], anchor="w", wraplength=1400, justify="left")
        self.voice_status.pack(fill="x")
        self.speed.trace_add("write", lambda *_: self._speed_changed())
        self._update_voice_status()

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(2, 8))
        left = ttk.Frame(body, width=250, padding=8)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)
        ttk.Label(left, text="CHAPTERS", style="Section.TLabel", padding=(2, 4)).pack(anchor="w")
        self.chapter_list = tk.Listbox(left, activestyle="dotbox", exportselection=False, takefocus=True)
        self.chapter_list.configure(bg=colors["panel"], fg=colors["text"], selectbackground=colors["accent_dark"],
                                    selectforeground="white", borderwidth=0, highlightthickness=2,
                                    highlightbackground=colors["panel"], highlightcolor="#58d6a3",
                                    font=("Segoe UI", 10), relief="flat")
        self.chapter_list.pack(fill="both", expand=True)
        self.chapter_list.bind("<<ListboxSelect>>", self.select_chapter)
        ttk.Label(left, text="NEXT CHAPTER", style="Section.TLabel", padding=(2, 12, 2, 2)).pack(anchor="w")
        self.prefetch_info = ttk.Label(left, text="Waiting for playback", padding=(2, 4), wraplength=230, foreground=colors["muted"])
        self.prefetch_info.pack(fill="x")
        self.prefetch_bar = ttk.Progressbar(left, mode="determinate", maximum=100)
        self.prefetch_bar.pack(fill="x", padx=2, pady=(2, 8))
        self.preload_stop_btn = ttk.Button(
            left, text="Stop loading future chapters", command=self.stop_preload, state="disabled"
        )
        self.preload_stop_btn.pack(anchor="e", padx=2, pady=(0, 8))
        ttk.Label(right, text="READING", style="Section.TLabel", padding=(4, 4)).pack(anchor="w")
        ttk.Label(right, text="Click a paragraph to start there · the current paragraph is highlighted", padding=(4, 0, 4, 7), foreground=colors["muted"]).pack(anchor="w")
        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True)
        self.text = tk.Text(text_frame, wrap="word", font=(self.text_font_family, self.text_font_size),
                            padx=18, pady=14, spacing3=4, undo=False, takefocus=True)
        self.text.configure(bg=colors["editor"], fg=colors["text"], insertbackground="white",
                            selectbackground="#315c78", relief="flat", borderwidth=0,
                            highlightthickness=2, highlightbackground=colors["editor"],
                            highlightcolor="#58d6a3")
        scroll = ttk.Scrollbar(text_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text.tag_configure("current", background="#254a47", foreground="#ffffff")
        self.text.tag_configure("current_word", background="#f4c95d", foreground="#172335")
        self.text.tag_configure("chapter_title", font=("Segoe UI", 17, "bold"), foreground=colors["accent"], spacing1=10)
        self.text.bind("<Button-1>", self.click_paragraph)

        bottom = ttk.Frame(self.root, padding=(14, 4, 14, 12))
        bottom.pack(fill="x")
        self.play_btn = ttk.Button(bottom, text="▶ Play chapter", command=self.toggle_play, underline=2)
        self.play_btn.pack(side="left")
        ttk.Button(bottom, text="■ Stop", command=self.stop_audio).pack(side="left", padx=6)
        ttk.Button(bottom, text="📱 Play on phone", command=self.show_phone_player).pack(side="left", padx=(6, 12))
        ttk.Button(bottom, text="◀ Previous chapter", command=lambda: self.step_chapter(-1)).pack(side="left", padx=(12, 4))
        ttk.Button(bottom, text="Next chapter ▶", command=lambda: self.step_chapter(1)).pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        self.status = ttk.Label(bottom, text="Ready", width=34, anchor="e", foreground=colors["muted"])
        self.status.pack(side="right")
        self.shortcut_hint = ttk.Label(
            self.root,
            text="Shortcuts: Ctrl+O open • Space play/stop • Left/Right chapter • Ctrl+Plus/Minus text size",
            padding=(12, 0, 12, 6),
            foreground=colors["muted"],
            anchor="w",
        )
        self.shortcut_hint.pack(fill="x")
        self._bind_accessibility_shortcuts()
        self.open_btn.focus_set()

    def _bind_accessibility_shortcuts(self):
        """Provide predictable keyboard access without changing playback or saved data."""
        self.root.bind_all("<Control-o>", lambda event: self._dispatch_shortcut(event, "open", self.open_novel))
        self.root.bind_all("<space>", lambda event: self._dispatch_shortcut(event, "play", self.toggle_play))
        self.root.bind_all("<Escape>", lambda event: self._dispatch_shortcut(event, "stop", self.stop_audio))
        self.root.bind_all("<Left>", lambda event: self._dispatch_shortcut(event, "chapter", self.step_chapter, -1))
        self.root.bind_all("<Right>", lambda event: self._dispatch_shortcut(event, "chapter", self.step_chapter, 1))
        for sequence in ("<Control-plus>", "<Control-equal>"):
            self.root.bind_all(sequence, lambda event: self._dispatch_shortcut(event, "scale", self._adjust_text_scale, 1))
        self.root.bind_all("<Control-minus>", lambda event: self._dispatch_shortcut(event, "scale", self._adjust_text_scale, -1))
        self.root.bind_all("<Control-0>", lambda event: self._dispatch_shortcut(event, "scale", self._reset_text_scale))

    def _dispatch_shortcut(self, event, shortcut, callback, *args):
        focused = self.root.focus_get()
        focused_class = focused.winfo_class() if focused is not None else ""
        if shortcut == "play" and focused_class in {"Entry", "TEntry", "TCombobox"}:
            return None
        if shortcut == "chapter" and focused_class in {"Entry", "TEntry", "TCombobox", "Text"}:
            return None
        callback(*args)
        return "break"

    def _adjust_text_scale(self, direction: int):
        self.text_font_size = min(24, max(10, self.text_font_size + (2 * direction)))
        self.text.configure(font=(self.text_font_family, self.text_font_size))
        self.status.configure(text=f"Text size {self.text_font_size}px")

    def _reset_text_scale(self):
        self.text_font_size = 13
        self.text.configure(font=(self.text_font_family, self.text_font_size))
        self.status.configure(text="Text size reset")

    def open_novel(self):
        if self.preloading:
            messagebox.showinfo("Audio preload is running", "Let the current novel finish preloading before opening another TXT file.")
            return
        name = filedialog.askopenfilename(title="Select a novel TXT or PDF", filetypes=[("TXT and PDF files", "*.txt *.pdf"), ("Text files", "*.txt"), ("PDF files", "*.pdf")])
        if not name:
            return
        self._audio_load_generation += 1
        try:
            if Path(name).suffix.lower() == ".pdf":
                if fitz is None:
                    raise RuntimeError("PDF support is unavailable because PyMuPDF could not be imported.")
                with fitz.open(name) as pdf:
                    content = "\n\n".join(page.get_text("text") for page in pdf)
            else:
                content = Path(name).read_text(encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror("Could not open file", str(exc))
            return
        self.stop_audio()
        self.novel_path = Path(name)
        self.chapters = split_novel(content)
        self.audio_cache_counts.clear()
        self.audio_cache_chapters.clear()
        self.audio_cache_speed = None
        self.prefetch_ready.clear()
        self.prefetching.clear()
        record = self._novel_record()
        self.audio_folder = Path(record.get("audio_folder") or (self.output_root / self._book_folder_name()))
        self._refresh_audio_cache_counts()
        self.chapter_idx = min(max(0, int(record.get("chapter_idx", 0))), len(self.chapters) - 1)
        self.resume_offset = max(0.0, float(record.get("offset", 0.0)))
        saved_voice = record.get("voice")
        for label, voice_id in VOICE_OPTIONS.items():
            if saved_voice == voice_id:
                self.voice_label.set(label)
                break
        self._update_voice_status()
        if record.get("speed"):
            self.speed.set(str(record["speed"]))
        self.file_label.configure(text=self.novel_path.name)
        self.chapter_list.delete(0, tk.END)
        for title, _ in self.chapters:
            self.chapter_list.insert(tk.END, title[:55])
        self.show_chapter()
        cached_audio = self._find_cached_audio(self.chapter_idx)
        if cached_audio:
            self._load_audio(cached_audio, start_playback=False, voice_id=VOICE_OPTIONS.get(self.voice_label.get()))
            self.offset = min(self.resume_offset, self.duration)
            self.play_btn.configure(text="▶ Resume saved place" if self.offset else "▶ Play chapter")
            self.status.configure(text=f"Restored Chapter {self.chapter_idx + 1}; cached audio ready")
        # Preload from the saved/current chapter first, then continue forward.
        if self.model_dir and self.model_dir.is_dir():
            self._start_novel_preload()

    def _novel_record(self) -> dict:
        if not self.novel_path:
            return {}
        novels = self.settings.setdefault("novels", {})
        return novels.setdefault(str(self.novel_path.resolve()), {})

    def _book_folder_name(self) -> str:
        assert self.novel_path
        name = re.sub(r'[<>:"/\\|?*]+', "_", self.novel_path.stem).strip(" ._")
        return (name[:90] or "Novel") + "_audio"

    @staticmethod
    def _backup_corrupt_settings(settings_path: Path) -> Path | None:
        """Preserve malformed settings beside the original without overwriting backups."""
        backup_path = settings_path.with_name(settings_path.name + ".corrupt")
        suffix = 1
        while backup_path.exists():
            backup_path = settings_path.with_name(settings_path.name + f".corrupt.{suffix}")
            suffix += 1
        try:
            shutil.copy2(settings_path, backup_path)
            return backup_path
        except OSError:
            return None

    def _save_settings(self):
        temporary_path = None
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings["schema_version"] = SETTINGS_SCHEMA_VERSION
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", prefix=self.settings_path.name + ".",
                suffix=".tmp", dir=self.settings_path.parent, delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(json.dumps(self.settings, ensure_ascii=False, indent=2, allow_nan=False))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.settings_path)
            temporary_path = None
        except OSError as exc:
            self.status.configure(text=f"Could not save settings: {exc}")
        except (TypeError, ValueError) as exc:
            self.status.configure(text=f"Could not save invalid settings: {exc}")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _save_progress(self, offset: float | None = None):
        if not self.novel_path or not self.chapters:
            return
        record = self._novel_record()
        record.update({
            "chapter_idx": self.chapter_idx,
            "offset": _bounded_float(self.offset if offset is None else offset, 0.0, 1e12) or 0.0,
            "audio_folder": str(self.audio_folder) if self.audio_folder else "",
            "voice": VOICE_OPTIONS.get(self.voice_label.get(), "af_nicole"),
            "speed": _bounded_float(self.speed.get(), MIN_SPEED, MAX_SPEED) or 1.0,
        })
        self._save_settings()

    def choose_audio_folder(self):
        initial = str(self.output_root if self.output_root.exists() else Path.home())
        path = filedialog.askdirectory(title="Choose the parent folder for all novel audio folders", initialdir=initial)
        if path:
            selected = Path(path)
            is_book_folder = bool(
                self.novel_path and selected.name.casefold() == self._book_folder_name().casefold()
            )
            next_output_root = selected.parent if is_book_folder else selected
            next_audio_folder = selected if is_book_folder else (
                next_output_root / self._book_folder_name() if self.novel_path else None
            )
            path_to_check = next_audio_folder or next_output_root
            issue = _directory_status(next_output_root, allow_missing=True)
            if issue is None:
                issue = _directory_status(path_to_check, allow_missing=True)
            if issue:
                self.status.configure(text=f"Audio folder unavailable ({issue}): {path_to_check}")
                return
            if self.novel_path:
                record = self._novel_record()
                history = record.setdefault("audio_folder_history", [])
                for previous in self._audio_search_folders():
                    previous_text = str(previous)
                    if previous_text != str(selected) and previous_text not in history:
                        history.append(previous_text)
            self.output_root = next_output_root
            chosen_book_folder = next_audio_folder
            self.settings["output_root"] = str(self.output_root)
            if self.novel_path:
                self.audio_folder = chosen_book_folder
                self._save_progress(offset=self.offset)
                self._refresh_audio_cache_counts()
            else:
                self._save_settings()
            self.prefetch_info.configure(text=f"Audio folders will be created in: {self.output_root}")
            messagebox.showinfo("Audio library location saved", f"Each novel will get its own folder inside:\n\n{self.output_root}\n\nExisting audio stays where it is.")

    def choose_model_dir(self):
        path = filedialog.askdirectory(title="Choose the folder containing Kokoro model and voice files")
        if path:
            self.model_dir = Path(path)
            self._renderer_content_fingerprints.clear()
            self.settings["model_dir"] = str(self.model_dir)
            self._save_settings()
            model_ok = (self.model_dir / "kokoro-v1.0.onnx").is_file()
            voices_ok = (self.model_dir / "voices-v1.0.bin").is_file()
            package_ok = importlib.util.find_spec("kokoro_onnx") is not None
            missing = []
            if not model_ok:
                missing.append("kokoro-v1.0.onnx")
            if not voices_ok:
                missing.append("voices-v1.0.bin")
            if not package_ok:
                missing.append("kokoro_onnx importable in this Python")
            if missing:
                message = "Not ready. Missing: " + ", ".join(missing)
                self.model_status.configure(text="Kokoro setup incomplete", foreground="#b42318")
                self.status.configure(text="Kokoro setup incomplete")
                messagebox.showwarning("Kokoro files/setup not confirmed", message + "\n\nThe selected folder is:\n" + str(self.model_dir))
            else:
                self.model_status.configure(text="✓ Kokoro and both model files found", foreground="#16733a")
                self.status.configure(text="Kokoro ready: " + str(self.model_dir))
                messagebox.showinfo("Kokoro folder confirmed", "Found the CLI package, model, and voice file in the selected Python/folder setup:\n\n" + str(self.model_dir))

    def select_chapter(self, _event=None):
        selected = self.chapter_list.curselection()
        if selected and selected[0] != self.chapter_idx:
            self._audio_load_generation += 1
            self.stop_audio()
            self.chapter_idx = selected[0]
            self.show_chapter()
            self.resume_offset = 0.0
            self._save_progress(offset=0.0)
            self._restart_preload_from_selected_chapter()

    def _restart_preload_from_selected_chapter(self):
        if self.preloading:
            active = next(iter(getattr(self, "prefetching", set())), None)
            if active is None:
                message = "Background preload continues in its current order; changing chapters will not restart it"
            else:
                message = (f"Background preload continues with Chapter {active + 1}; "
                           "changing chapters will not restart it")
            self.prefetch_info.configure(text=message)
            return
        if not self.chapters or not self.model_dir or not self.novel_path:
            return
        self._start_novel_preload()

    def show_chapter(self):
        if not self.chapters:
            return
        title, body = self.chapters[self.chapter_idx]
        paragraphs = [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
        self.lines = paragraphs
        self.timing_line_map = [i for i, paragraph in enumerate(paragraphs) if speech_text(paragraph)]
        if not self.lines:
            self.lines = [body.strip() or title]
        self.line_starts = []
        total_words = sum(max(1, len(re.findall(r"\S+", line))) for line in self.lines)
        cursor = 0.0
        for line in self.lines:
            self.line_starts.append(cursor)
            cursor += max(1, len(re.findall(r"\S+", line))) / max(1, total_words)
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, title + "\n\n", "chapter_title")
        for line in self.lines:
            self.text.insert(tk.END, line + "\n\n")
        self.text.configure(state="disabled")
        self._highlight_range = None
        self._highlight_word_range = None
        self.chapter_list.selection_clear(0, tk.END)
        self.chapter_list.selection_set(self.chapter_idx)
        self._refresh_chapter_list()
        self.chapter_list.see(self.chapter_idx)
        self.progress["value"] = 0
        self.status.configure(text=f"Chapter {self.chapter_idx + 1} of {len(self.chapters)}")

    def _cache_file(self, chapter_idx: int | None = None, voice_id: str | None = None, speed_value: str | None = None) -> Path:
        assert self.novel_path
        chapter_idx = self.chapter_idx if chapter_idx is None else chapter_idx
        voice = voice_id or VOICE_OPTIONS[self.voice_label.get()]
        speed_value = speed_value or self.speed.get()
        try:
            speed_value = str(float(speed_value))
        except ValueError:
            pass
        identity = self._renderer_cache_identity()
        digest = hashlib.sha256((RENDER_CACHE_VERSION + self.novel_path.name + str(chapter_idx) + self.chapters[chapter_idx][1] + voice + speed_value + identity).encode("utf-8")).hexdigest()[:20]
        folder = self.audio_folder or (self.output_root / self._book_folder_name())
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"chapter_{chapter_idx + 1:04d}_{digest}.wav"

    def _cache_names(self, chapter_idx: int, voice_id: str, speed_value: str) -> tuple[str, ...]:
        try:
            speed_value = str(float(speed_value))
        except ValueError:
            pass
        chapter_text = self.chapters[chapter_idx][1]
        identity = self._renderer_cache_identity()
        cache_hash = hashlib.sha256((RENDER_CACHE_VERSION + self.novel_path.name + str(chapter_idx) + chapter_text + voice_id + speed_value + identity).encode("utf-8")).hexdigest()[:20]
        current_name = f"chapter_{chapter_idx + 1:04d}_{cache_hash}.wav"
        # Releases before renderer identity was added used the same stable
        # inputs except for the identity suffix. Keep those files reusable.
        prior_hash = hashlib.sha256((RENDER_CACHE_VERSION + self.novel_path.name + str(chapter_idx) + chapter_text + voice_id + speed_value).encode("utf-8")).hexdigest()[:20]
        prior_identity_name = f"chapter_{chapter_idx + 1:04d}_{prior_hash}.wav"
        legacy_name = self._legacy_cache_file(chapter_idx, voice_id, speed_value).name
        return tuple(dict.fromkeys((current_name, prior_identity_name, legacy_name)))

    def _renderer_cache_identity(self) -> str:
        """Identify model and renderer bytes while hashing each unchanged file once per reader session."""
        components = [RENDER_CACHE_VERSION, TEXT_NORMALIZATION_VERSION, RENDER_AUDIO_FORMAT]
        try:
            components.append("kokoro-onnx=" + importlib.metadata.version("kokoro-onnx"))
        except importlib.metadata.PackageNotFoundError:
            components.append("kokoro-onnx=missing")
        paths = []
        if self.model_dir:
            paths.extend((self.model_dir / "kokoro-v1.0.onnx", self.model_dir / "voices-v1.0.bin"))
        else:
            components.append("model-unselected")
        paths.append(Path(__file__).with_name("kokoro_paragraph_worker.py"))
        for path in paths:
            try:
                stat = path.stat()
                resolved = str(path.resolve())
                fingerprint_cache = getattr(self, "_renderer_content_fingerprints", None)
                if fingerprint_cache is None:
                    fingerprint_cache = self._renderer_content_fingerprints = {}
                cached = fingerprint_cache.get(resolved)
                if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                    content_hash = cached[2]
                else:
                    content_hash = _sha256_path(path)
                    fingerprint_cache[resolved] = (stat.st_size, stat.st_mtime_ns, content_hash)
                components.append(f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}:{content_hash}")
            except OSError:
                components.append(f"{path}:missing")
        return "|".join(components)

    def _audio_search_folders(self) -> list[Path]:
        if not self.novel_path:
            return []
        folders: list[Path] = []
        if self.audio_folder:
            folders.append(self.audio_folder)
            # Older setup selected the book folder as the library root, which
            # left WAVs in its parent and created a duplicate nested folder.
            if self.audio_folder.name.casefold() == self.audio_folder.parent.name.casefold():
                folders.append(self.audio_folder.parent)
            folders.append(self.audio_folder / self.audio_folder.name)
        record = self._novel_record()
        folders.extend(Path(item) for item in record.get("audio_folder_history", []) if item)
        folders.append(self.output_root / self._book_folder_name())
        folders.append(Path.home() / "Documents" / "Kokoro Audio" / self._book_folder_name())
        unique: list[Path] = []
        seen = set()
        for folder in folders:
            key = os.path.normcase(os.path.abspath(folder))
            if key not in seen:
                seen.add(key)
                unique.append(folder)
        return unique

    def _legacy_cache_file(self, chapter_idx: int, voice_id: str | None = None, speed_value: str | None = None) -> Path:
        assert self.novel_path
        voice = voice_id or VOICE_OPTIONS[self.voice_label.get()]
        speed_value = speed_value or self.speed.get()
        try:
            speed_value = str(float(speed_value))
        except ValueError:
            pass
        digest = hashlib.sha1((self.novel_path.name + str(chapter_idx) + self.chapters[chapter_idx][1] + voice + speed_value).encode("utf-8")).hexdigest()[:16]
        folder = self.audio_folder or (self.output_root / self._book_folder_name())
        return folder / f"chapter_{chapter_idx + 1:04d}_{digest}.wav"

    def _find_cached_audio(self, chapter_idx: int, voice_id: str | None = None, speed_value: str | None = None) -> Path | None:
        voice_id = voice_id or VOICE_OPTIONS[self.voice_label.get()]
        speed_value = speed_value or self._normalized_speed()
        names = self._cache_names(chapter_idx, voice_id, speed_value)
        for folder in self._audio_search_folders():
            for name in names:
                candidate = folder / name
                if self._cached_audio_is_valid(candidate):
                    if not Path(str(candidate) + ".timing.json").exists():
                        self._create_estimated_timings(chapter_idx, candidate)
                    return candidate
        # Older cache versions changed their hash inputs more than once.
        # When the exact name is unavailable, reuse a complete chapter WAV
        # from this novel's folder instead of synthesizing it again.
        chapter_prefix = f"chapter_{chapter_idx + 1:04d}_"
        for folder in self._audio_search_folders():
            try:
                candidates = sorted(
                    (candidate for candidate in folder.glob(f"{chapter_prefix}*.wav")
                     if candidate.is_file()),
                    key=lambda candidate: candidate.stat().st_mtime_ns,
                    reverse=True,
                )
            except OSError:
                continue
            for candidate in candidates:
                if NovelReader._legacy_audio_is_usable(candidate):
                    if not self._cached_audio_is_valid(candidate, allow_stale_completion=True):
                        timing_path = Path(str(candidate) + ".timing.json")
                        timing_path.unlink(missing_ok=True)
                        self._create_estimated_timings(chapter_idx, candidate)
                    return candidate
        return None

    @staticmethod
    def _legacy_audio_is_usable(audio_path: Path) -> bool:
        if sf is None or not audio_path.is_file():
            return False
        try:
            info = sf.info(audio_path)
            return (
                info.frames > 0
                and info.samplerate > 0
                and math.isfinite(float(info.duration))
                and info.duration > 0
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            return False

    @staticmethod
    def _cached_audio_is_valid(audio_path: Path, allow_stale_completion: bool = False) -> bool:
        """Reject unreadable WAVs and sidecars that record failed paragraphs."""
        if sf is None or not audio_path.is_file():
            return False
        try:
            pending_path = Path(str(audio_path) + ".pending")
            if pending_path.exists():
                return False
            info = sf.info(audio_path)
            duration = float(info.duration)
            if info.frames <= 0 or info.samplerate <= 0 or not math.isfinite(duration) or duration <= 0:
                return False
            timing_path = Path(str(audio_path) + ".timing.json")
            completion_path = Path(str(audio_path) + ".complete.json")
            if completion_path.exists() and not allow_stale_completion:
                if not completion_path.is_file() or not timing_path.is_file():
                    return False
                completion = json.loads(completion_path.read_text(encoding="utf-8"))
                if (not isinstance(completion, dict)
                        or completion.get("format") != "kokoro-chapter-commit-v1"
                        or completion.get("complete") is not True
                        or not isinstance(completion.get("renderer_identity"), str)
                        or not completion.get("renderer_identity")
                        or not isinstance(completion.get("audio_sha256"), str)
                        or len(completion["audio_sha256"]) != 64
                        or completion.get("audio_size") != audio_path.stat().st_size
                        or _sha256_path(audio_path) != completion["audio_sha256"]
                        or completion.get("timing_sha256") != hashlib.sha256(timing_path.read_bytes()).hexdigest()):
                    return False
            if not timing_path.exists():
                return True  # Legacy cache: timings can be estimated when loaded.
            if not timing_path.is_file():
                return False
            timings = json.loads(timing_path.read_text(encoding="utf-8"))
            if not isinstance(timings, list) or not timings:
                return False
            previous_start = -1.0
            has_audio_interval = False
            for timing in timings:
                if not isinstance(timing, dict):
                    return False
                start = float(timing["start"])
                end = float(timing["end"])
                if (not math.isfinite(start) or not math.isfinite(end)
                        or start < 0 or end < start or end > duration + 0.05
                        or start < previous_start):
                    return False
                if str(timing.get("text", "")).strip() and end <= start:
                    return False
                has_audio_interval = has_audio_interval or end > start
                previous_start = start
            return has_audio_interval
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def _speed_changed(self):
        self._update_voice_status()
        if self.novel_path:
            if self.cache_scan_after is not None:
                self.root.after_cancel(self.cache_scan_after)
            self.cache_scan_after = self._schedule_after(450, self._refresh_audio_cache_counts)

    def _refresh_audio_cache_counts(self):
        if not self.novel_path or not self.chapters:
            return
        self.cache_scan_after = None
        self.cache_scan_generation += 1
        generation = self.cache_scan_generation
        novel_path = str(self.novel_path.resolve())
        chapters = list(self.chapters)
        folders = self._audio_search_folders()
        speed_key = self._normalized_speed()
        names_by_voice = {
            voice_id: [self._cache_names(chapter_idx, voice_id, speed_key) for chapter_idx in range(len(chapters))]
            for voice_id in VOICE_OPTIONS.values()
        }
        self.audio_cache_counts = {}
        self.audio_cache_chapters = {}
        self.audio_cache_speed = None
        self._update_voice_status()

        def scan():
            ready_by_voice = {}
            for voice_id, chapter_names in names_by_voice.items():
                ready = set()
                for chapter_idx, names in enumerate(chapter_names):
                    if any(self._cached_audio_is_valid(folder / name) for folder in folders for name in names):
                        ready.add(chapter_idx)
                if voice_id == VOICE_OPTIONS.get(self.voice_label.get()):
                    ready.update(
                        chapter_idx for chapter_idx in range(len(chapters))
                        if chapter_idx not in ready and self._find_cached_audio(
                            chapter_idx, voice_id, speed_key
                        ) is not None
                    )
                ready_by_voice[voice_id] = ready
            self._schedule_after(0, lambda: self._apply_audio_cache_counts(generation, novel_path, speed_key, ready_by_voice))

        self._start_daemon(scan)

    def _apply_audio_cache_counts(self, generation: int, novel_path: str, speed_key: str, ready_by_voice: dict[str, set[int]]):
        if generation != self.cache_scan_generation or not self.novel_path or str(self.novel_path.resolve()) != novel_path:
            return
        for voice_id, ready_chapters in self.audio_cache_chapters.items():
            if voice_id == self.prefetch_voice_id and self.prefetch_speed != speed_key:
                continue
            ready_by_voice.setdefault(voice_id, set()).update(ready_chapters)
        if self.prefetch_voice_id and self.prefetch_speed == speed_key:
            ready_by_voice.setdefault(self.prefetch_voice_id, set()).update(self.prefetch_ready)
        self.audio_cache_chapters = ready_by_voice
        self.audio_cache_counts = {voice_id: len(chapters) for voice_id, chapters in ready_by_voice.items()}
        self.audio_cache_speed = speed_key
        self._update_voice_status()

    def _create_estimated_timings(self, chapter_idx: int, audio_path: Path):
        sidecar = Path(str(audio_path) + ".timing.json")
        if sidecar.exists():
            return
        paragraphs = [speech_text(p) for p in re.split(r"\n\s*\n", self.chapters[chapter_idx][1])]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            return
        duration = sf.info(audio_path).duration
        weights = [max(1, len(re.findall(r"\S+", p))) for p in paragraphs]
        total = sum(weights)
        timings = []
        cursor = 0.0
        for text, weight in zip(paragraphs, weights):
            end = cursor + duration * weight / total
            timings.append({"text": text, "start": cursor, "end": end})
            cursor = end
        sidecar.write_text(json.dumps(timings, ensure_ascii=False), encoding="utf-8")

    def toggle_play(self):
        if not self.chapters:
            messagebox.showinfo("Open a novel", "Choose the TXT file you saved from the chapter collector first.")
            return
        if self.playing:
            self.pause_audio()
            return
        selected_voice = VOICE_OPTIONS.get(self.voice_label.get())
        if self.audio is not None and self.offset < self.duration and self.loaded_audio_voice_id == selected_voice:
            self._play_from(self.offset)
        else:
            self._generate_and_play()

    def _voice_changed(self, _event=None):
        if self.playing:
            self.pause_audio()
        selected_voice_id = VOICE_OPTIONS.get(self.voice_label.get())
        if self.preloading and selected_voice_id != self.prefetch_voice_id:
            self.preload_restart_requested = True
            if self.preload_cancel_event is not None:
                self.preload_cancel_event.set()
            process = self.preload_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            self.prefetch_info.configure(text=f"Switching background preload to {self.voice_label.get()}… completed files are kept")
        self._update_voice_status()
        if self.chapters and not self.preloading:
            self._schedule_after(150, self._start_novel_preload)

    def _update_voice_status(self):
        if not hasattr(self, "voice_status"):
            return
        selected = self.voice_label.get()
        selected_voice_id = VOICE_OPTIONS.get(selected)
        loaded = next((label for label, voice_id in VOICE_OPTIONS.items() if voice_id == self.loaded_audio_voice_id), None)
        if loaded is None:
            voice_text = f"Selected voice: {selected}  •  Current chapter audio: not loaded"
        elif loaded == selected:
            voice_text = f"Selected voice: {selected}  •  Current chapter audio: {loaded} (matches)"
        else:
            voice_text = f"Selected voice: {selected}  •  Loaded chapter audio: {loaded} — press Play to switch"
        aligned_words = sum(len(words) for words in self.word_spans)
        word_text = f"Word sync: saved audio timings loaded ({aligned_words} words)" if aligned_words else "Word sync: no saved word timings for this audio yet"
        speed_key = self._normalized_speed()
        total = len(self.chapters)
        if self.audio_cache_speed == speed_key:
            counts = self.audio_cache_counts
            selected_count = counts.get(selected_voice_id, 0)
            ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
            top_voice_id, top_count = ranked[0] if ranked else (selected_voice_id, 0)
            top_voice = next((label for label, voice_id in VOICE_OPTIONS.items() if voice_id == top_voice_id), selected)
            by_voice = " • ".join(f"{label.split(' (')[0]} {counts.get(voice_id, 0)}" for label, voice_id in VOICE_OPTIONS.items())
            most_saved = f"most saved: {top_voice} ({top_count})" if top_count else "no saved chapter audio found in searched folders"
            cache_text = f"Saved chapter audio at {speed_key}×: {selected_count} / {total} for {selected}; {most_saved}\nBy voice: {by_voice}"
        else:
            cache_text = f"Scanning saved chapter audio for {speed_key}×…"
        if self.preloading and self.prefetch_voice_id != selected_voice_id:
            preload_label = next((label for label, voice_id in VOICE_OPTIONS.items() if voice_id == self.prefetch_voice_id), "another voice")
            cache_text += f"  •  Background preload is using {preload_label}"
        self.voice_status.configure(text=voice_text + "\n" + word_text + "\n" + cache_text)

    def _normalized_speed(self) -> str:
        raw = self.speed.get().strip()
        parsed = _bounded_float(raw, MIN_SPEED, MAX_SPEED)
        return str(parsed) if parsed is not None else raw

    def _generate_and_play(self):
        if self.busy:
            return
        if not self.model_dir:
            messagebox.showinfo("Choose Kokoro folder", "Choose the folder containing kokoro-v1.0.onnx and voices-v1.0.bin first.")
            return
        if not (self.model_dir / "kokoro-v1.0.onnx").is_file() or not (self.model_dir / "voices-v1.0.bin").is_file() or importlib.util.find_spec("kokoro_onnx") is None:
            messagebox.showerror("Kokoro setup not ready", "Choose the folder containing kokoro-v1.0.onnx and voices-v1.0.bin, and make sure kokoro_onnx is importable in the Python environment running this reader.")
            return
        speed = _bounded_float(self.speed.get(), MIN_SPEED, MAX_SPEED)
        if speed is None:
            messagebox.showerror("Invalid speed", f"Enter a finite speed between {MIN_SPEED} and {MAX_SPEED}.")
            return
        cached = self._find_cached_audio(self.chapter_idx)
        if cached:
            self._load_audio(cached, voice_id=VOICE_OPTIONS[self.voice_label.get()])
            return
        target = self._cache_file()
        voice_id = VOICE_OPTIONS[self.voice_label.get()]
        next_idx = self.chapter_idx
        if self.preloading:
            self.status.configure(text="Background audio preparation is running; playback will use it when ready…")
            self._schedule_after(1000, self._generate_and_play)
            return
        input_file = self._cache_file(next_idx).with_suffix(".input.json")
        cleaned_paragraphs = [speech_text(line) for line in re.split(r"\n\s*\n", self.chapters[self.chapter_idx][1])]
        cleaned_paragraphs = [p for p in cleaned_paragraphs if p]
        if not cleaned_paragraphs:
            messagebox.showwarning("Nothing to read", "This chapter contains no speakable letters or numbers after decorative symbols are skipped.")
            return
        input_file.write_text(json.dumps(cleaned_paragraphs, ensure_ascii=False), encoding="utf-8")
        self.busy = True
        self.play_btn.configure(state="disabled")
        self.status.configure(text="Starting Kokoro… first chapter can take a while")
        self._show_generation_window()
        def work():
            worker = Path(__file__).with_name("kokoro_paragraph_worker.py")
            command = [sys.executable, str(worker), "single", "--model", str(self.model_dir / "kokoro-v1.0.onnx"), "--voices", str(self.model_dir / "voices-v1.0.bin"), "--voice", voice_id, "--speed", str(speed), "--cache-identity", self._renderer_cache_identity(), "--chapter", str(next_idx + 1), "--input", str(input_file), "--output", str(target)]
            try:
                child_env = os.environ.copy()
                child_env["PYTHONIOENCODING"] = "utf-8"
                child_env["PYTHONUTF8"] = "1"
                process = subprocess.Popen(command, cwd=self.model_dir, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=KOKORO_WORKER_PRIORITY)
                self.generation_process = process
                stderr_lines = []
                stderr_thread = threading.Thread(target=_drain_text_stream, args=(process.stderr, stderr_lines), daemon=True)
                stderr_thread.start()
                recent = []
                protocol_errors = []
                expected_job_id = None
                assert process.stdout is not None
                for output_line in process.stdout:
                    line = output_line.strip()
                    if line:
                        recent.append(line)
                        recent = recent[-3:]
                        try:
                            event = json.loads(line)
                            if (not isinstance(event, dict) or event.get("protocol") != 1
                                    or not isinstance(event.get("job_id"), str)):
                                protocol_errors.append(f"Unsupported or malformed worker event: {line}")
                            elif expected_job_id is None:
                                expected_job_id = event["job_id"]
                            elif event["job_id"] != expected_job_id:
                                protocol_errors.append(f"Worker event used an unexpected job ID: {line}")
                            elif event.get("event") == "paragraph":
                                self._schedule_after(0, lambda p=event: self._update_generation_progress(p["paragraph"], p["total"]))
                        except (ValueError, KeyError, TypeError):
                            pass
                        self._schedule_after(0, lambda status="Kokoro: " + line[-100:]: self.status.configure(text=status))
                return_code = process.wait(timeout=7200)
                stderr_thread.join(timeout=2)
                if self.generation_process is process:
                    self.generation_process = None
                if protocol_errors:
                    raise RuntimeError("\n".join(protocol_errors[-3:]))
                if return_code != 0 or any("Error:" in item or "Traceback" in item for item in recent):
                    raise RuntimeError("\n".join(recent + stderr_lines[-3:]) or f"Kokoro exited with code {return_code}")
                input_file.unlink(missing_ok=True)
                self._schedule_after(0, lambda: self._generation_done(target, None, voice_id))
            except Exception as exc:
                if self.generation_process is not None and self.generation_process.poll() is not None:
                    self.generation_process = None
                self._schedule_after(0, lambda err=str(exc): self._generation_done(target, err))
        self._start_daemon(work)

    def _show_generation_window(self):
        if self.generation_window is not None:
            self.generation_window.destroy()
        window = tk.Toplevel(self.root)
        self.generation_window = window
        window.title("Generating chapter audio")
        window.geometry("470x190")
        window.resizable(False, False)
        window.transient(self.root)
        window.lift()
        chapter_title = self.chapters[self.chapter_idx][0]
        ttk.Label(window, text=f"Creating audio for Chapter {self.chapter_idx + 1} of {len(self.chapters)}", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18, pady=(18, 4))
        ttk.Label(window, text=chapter_title[:72], wraplength=430).pack(anchor="w", padx=18)
        self.generation_detail = ttk.Label(window, text="Starting Kokoro…")
        self.generation_detail.pack(anchor="w", padx=18, pady=(15, 4))
        self.generation_progress = ttk.Progressbar(window, mode="determinate", maximum=100, length=430)
        self.generation_progress.pack(padx=18, fill="x")
        self.generation_percent = ttk.Label(window, text="Waiting for chunk progress…")
        self.generation_percent.pack(anchor="e", padx=18, pady=(5, 12))
        self.generation_started = time.monotonic()

    def _update_generation_progress(self, current: int, total: int):
        if self.generation_window is None or not self.generation_window.winfo_exists():
            return
        percent = min(100, round(100 * current / max(1, total)))
        elapsed = int(time.monotonic() - self.generation_started)
        remaining = int((elapsed / max(1, current)) * max(0, total - current))
        self.generation_progress.configure(maximum=total, value=current)
        self.generation_detail.configure(text=f"Rendering paragraph {current} of {total}")
        self.generation_percent.configure(text=f"{percent}%  •  elapsed {elapsed // 60}m {elapsed % 60}s  •  approx. {remaining // 60}m {remaining % 60}s remaining")

    def _generation_done(self, path: Path, error: str | None, voice_id: str | None = None):
        self.busy = False
        self.play_btn.configure(state="normal")
        if self.generation_window is not None and self.generation_window.winfo_exists():
            self.generation_window.destroy()
        self.generation_window = None
        if error:
            self.status.configure(text="Audio generation failed")
            messagebox.showerror("Kokoro TTS error", error + "\n\nCheck that kokoro-tts is installed and the model files are in the selected folder.")
            return
        # A newly rendered chapter still needs to honor the saved resume point.
        # The cached-audio path already restores this position in _finish_audio_load.
        self._load_audio(path, voice_id=voice_id, start_offset=self.resume_offset)

    def _load_audio(self, path: Path, start_playback: bool = True, voice_id: str | None = None,
                    start_offset: float | None = None):
        self._audio_load_generation += 1
        generation = self._audio_load_generation
        self.play_btn.configure(state="disabled")
        self.status.configure(text="Loading chapter audio…")

        def load():
            try:
                audio, samplerate, audio_hash = _read_audio_for_playback(path)
                error = None
            except Exception as exc:
                audio, samplerate, audio_hash, error = None, 0, None, exc
            scheduled = self._schedule_after(
                0, lambda: self._finish_audio_load(
                    generation, path, audio, samplerate, audio_hash, start_playback, voice_id, error,
                    start_offset
                )
            )
            if scheduled is None:
                _release_audio_buffer(audio)

        try:
            self._start_daemon(load)
        except RuntimeError as exc:
            self.play_btn.configure(state="normal")
            messagebox.showerror("Audio error", str(exc))

    def _finish_audio_load(self, generation: int, path: Path, audio, samplerate: int, audio_hash: str | None,
                           start_playback: bool, voice_id: str | None, error: Exception | None,
                           start_offset: float | None = None):
        if self._closing or generation != self._audio_load_generation:
            _release_audio_buffer(audio)
            return
        self.play_btn.configure(state="normal")
        try:
            if error is not None:
                raise error
            if not _cache_audio_digest_matches(path, audio_hash):
                raise RuntimeError("Cached audio content does not match its completion manifest; regenerate this chapter.")
            old_audio = self.audio
            self.audio, self.samplerate = audio, samplerate
            self.current_audio_path = path
            self.phone_audio_speed = self._normalized_speed()
            if old_audio is not audio:
                _release_audio_buffer(old_audio)
            self.loaded_audio_voice_id = voice_id or VOICE_OPTIONS.get(self.voice_label.get())
            self._update_voice_status()
            self.duration = len(self.audio) / self.samplerate
            timing_file = Path(str(path) + ".timing.json")
            self.paragraph_timings = json.loads(timing_file.read_text(encoding="utf-8")) if timing_file.exists() else []
            self._rebuild_timing_lookup()
            self.word_spans = self._load_word_timings(path, timing_file, audio_hash=audio_hash)
            requested_offset = self.resume_offset if start_offset is None else max(0.0, start_offset)
            self.offset = min(requested_offset, self.duration)
            if start_playback and self.offset >= self.duration:
                self.offset = 0.0
            if self.loaded_audio_voice_id:
                ready_chapters = self.audio_cache_chapters.setdefault(self.loaded_audio_voice_id, set())
                ready_chapters.add(self.chapter_idx)
                self.audio_cache_counts[self.loaded_audio_voice_id] = len(ready_chapters)
            if self.prefetch_voice_id == self.loaded_audio_voice_id and self.prefetch_speed == self._normalized_speed():
                self.prefetch_ready.add(self.chapter_idx)
            self._update_voice_status()
            if start_playback:
                self._play_from(self.offset)
                self._start_prefetch_next()
        except Exception as exc:
            if self.audio is audio:
                self.audio = None
                self.current_audio_path = None
                self.loaded_audio_voice_id = None
            _release_audio_buffer(audio)
            messagebox.showerror("Audio error", str(exc))

    def _start_prefetch_next(self):
        self._start_novel_preload()

    def show_phone_player(self):
        if self.current_audio_path is None or not self.current_audio_path.is_file():
            messagebox.showinfo("Phone playback", "Play or load a chapter on this computer first.")
            return
        try:
            url = self.phone_audio_server.start()
        except OSError as exc:
            messagebox.showerror(
                "Phone playback unavailable",
                f"Could not open the local phone audio server on port {PHONE_CAST_PORT}.\n\n{exc}",
            )
            return
        self._schedule_phone_screen_off()
        dialog = tk.Toplevel(self.root)
        dialog.title("Play PageWave on your phone")
        dialog.geometry("560x260")
        dialog.transient(self.root)
        ttk.Label(dialog, text="On your phone, join the same Wi‑Fi network and open:", wraplength=520).pack(
            anchor="w", padx=20, pady=(20, 8)
        )
        url_var = tk.StringVar(value=url)
        url_entry = ttk.Entry(dialog, textvariable=url_var, state="readonly", width=72)
        url_entry.pack(fill="x", padx=20)
        ttk.Button(dialog, text="Copy link", command=lambda: self._copy_phone_url(url)).pack(
            anchor="e", padx=20, pady=(8, 14)
        )
        ttk.Label(
            dialog,
            text="Keep PageWave open while listening. PageWave will prevent Windows sleep while this server is active. Close it or use Stop server when finished.",
            wraplength=520,
        ).pack(anchor="w", padx=20)
        ttk.Button(dialog, text="Stop phone server", command=lambda: (self._cancel_phone_screen_off(), self.phone_audio_server.stop(), dialog.destroy())).pack(
            anchor="e", padx=20, pady=14
        )

    def _copy_phone_url(self, url: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.root.update()
        self.status.configure(text="Phone player link copied")

    def _find_phone_screen_off_shortcut(self) -> Path | None:
        candidates = [
            Path(__file__).with_name(PHONE_SCREEN_OFF_SHORTCUT_NAME),
            Path.cwd() / PHONE_SCREEN_OFF_SHORTCUT_NAME,
            Path.home() / "Desktop" / PHONE_SCREEN_OFF_SHORTCUT_NAME,
            Path.home() / "Downloads" / PHONE_SCREEN_OFF_SHORTCUT_NAME,
        ]
        return next((path for path in candidates if path.is_file()), None)

    def _schedule_phone_screen_off(self):
        self._cancel_phone_screen_off()
        shortcut = self._find_phone_screen_off_shortcut()
        if shortcut is None:
            if os.name != "nt":
                self.status.configure(text=f"Phone playback active; {PHONE_SCREEN_OFF_SHORTCUT_NAME} was not found")
                return
            self.status.configure(text="Phone playback active; screen will turn off in 5 minutes")
        self.phone_screen_off_shortcut = shortcut
        self.phone_screen_off_after = self._schedule_after(
            PHONE_SCREEN_OFF_DELAY_MS, self._run_phone_screen_off_shortcut
        )
        self.status.configure(text="Phone playback active; screen will turn off in 5 minutes")

    def _cancel_phone_screen_off(self):
        if self.phone_screen_off_after is not None:
            try:
                self.root.after_cancel(self.phone_screen_off_after)
            except tk.TclError:
                pass
        self.phone_screen_off_after = None
        self.phone_screen_off_shortcut = None

    def _run_phone_screen_off_shortcut(self):
        self.phone_screen_off_after = None
        shortcut = self.phone_screen_off_shortcut
        if not self.phone_audio_server.running or shortcut is None:
            return
        try:
            if shortcut is not None:
                os.startfile(str(shortcut))
            elif os.name == "nt":
                command = (
                    "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] "
                    "public static extern int PostMessage(int a, int b, int c, int d);' "
                    "-Name f -PassThru)::PostMessage(-1, 0x112, 0xF170, 2)"
                )
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                return
            self.status.configure(text="Phone playback active; screen turned off after 5 minutes")
        except OSError as exc:
            self.status.configure(text=f"Could not run screen-off shortcut: {exc}")

    def _phone_audio_state(self, requested_chapter: int | None = None) -> dict:
        chapter = self.chapter_idx if requested_chapter is None else requested_chapter
        next_path = None
        next_title = ""
        voice_id = self.loaded_audio_voice_id
        audio_path = self.current_audio_path if chapter == self.chapter_idx else None
        if voice_id and 0 <= chapter < len(self.chapters):
            if audio_path is None:
                audio_path = self._find_phone_cached_audio(chapter, voice_id)
        if voice_id and chapter + 1 < len(self.chapters):
            next_path = self._find_phone_cached_audio(chapter + 1, voice_id)
            next_title = self.chapters[chapter + 1][0]
        return {
            "chapter": chapter,
            "audio_path": audio_path,
            "next_path": next_path,
            "next_title": next_title,
            "auto_next": self.phone_auto_next_enabled,
        }

    def _find_phone_cached_audio(self, chapter_idx: int, voice_id: str) -> Path | None:
        """Resolve phone chapters using exact caches plus the desktop legacy fallback."""
        for speed_value in (self.phone_audio_speed, self._normalized_speed(), str(self.speed.get())):
            candidate = self._find_cached_audio(chapter_idx, voice_id, speed_value)
            if candidate is not None:
                return candidate
        prefix = f"chapter_{chapter_idx + 1:04d}_"
        for folder in self._audio_search_folders():
            try:
                candidates = sorted(folder.glob(f"{prefix}*.wav"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
            except OSError:
                continue
            for candidate in candidates:
                if self._legacy_audio_is_usable(candidate):
                    return candidate
        return None

    def _start_novel_preload(self):
        if self.preloading or not self.chapters or not self.model_dir or not self.novel_path:
            return
        if not (self.model_dir / "kokoro-v1.0.onnx").is_file() or not (self.model_dir / "voices-v1.0.bin").is_file():
            return
        cancel_event = threading.Event()
        active_idx = self.chapter_idx
        active_is_playing = self.busy or self.playing
        voice_id = VOICE_OPTIONS[self.voice_label.get()]
        speed = _bounded_float(self.speed.get(), MIN_SPEED, MAX_SPEED)
        if speed is None:
            self.prefetch_info.configure(text=f"Enter a finite speed between {MIN_SPEED} and {MAX_SPEED} to preload audio")
            return
        speed_key = str(speed)
        self.preloading = True
        self.preload_state = "running"
        self.preload_restart_requested = False
        self.preload_cancel_event = cancel_event
        if self.prefetch_voice_id != voice_id or self.prefetch_speed != speed_key:
            self.prefetch_ready.clear()
        self.prefetch_voice_id = voice_id
        self.prefetch_speed = speed_key
        self.preload_stop_btn.configure(state="normal")
        self._update_voice_status()
        self.prefetch_info.configure(
            text=f"Checking saved audio; preparing up to {PRELOAD_LOOKAHEAD_CHAPTERS} chapters ahead…"
        )
        self.prefetch_bar.configure(maximum=len(self.chapters), value=0)
        self._refresh_chapter_list()
        errors = []
        timing_folder = getattr(self, "audio_folder", None)
        if timing_folder is None and getattr(self, "output_root", None) is not None:
            timing_folder = self.output_root / self._book_folder_name()
        timing_log_path = Path(timing_folder) / "_preload_timings.jsonl" if timing_folder else None
        chapter_started_at: dict[int, float] = {}

        def finish_timing(chapter_number: int, result: str) -> float | None:
            started_at = chapter_started_at.pop(chapter_number, None)
            if started_at is None:
                return None
            elapsed = max(0.0, time.monotonic() - started_at)
            if timing_log_path is not None:
                try:
                    _append_preload_timing(timing_log_path, chapter_number, elapsed,
                                           result, voice_id, speed_key)
                except OSError:
                    # Timing telemetry must never turn a rendered chapter into a failure.
                    pass
            return elapsed

        def run_preload():
            jobs = []
            missing_indices = []
            for idx in _preload_window(len(self.chapters), active_idx):
                if cancel_event.is_set():
                    break
                if (idx == active_idx and active_is_playing) or (idx == self.chapter_idx and (self.busy or self.playing)):
                    continue
                cached = self._find_cached_audio(idx, voice_id, str(speed))
                if cached:
                    self._schedule_after(0, lambda chapter=idx: self._preload_chapter_done(
                        chapter, None, cached=True))
                    continue
                missing_indices.append(idx)
            # Start with the selected/resume chapter, then render only a small
            # forward window so long novels do not consume resources all at once.
            order = _preload_order(missing_indices, active_idx)
            if order:
                first_chapter = order[0] + 1
                self._schedule_after(0, lambda first=first_chapter: self._preload_queue_started(first, len(self.chapters)))
            for idx in order:
                if cancel_event.is_set():
                    break
                target = self._cache_file(idx, voice_id, str(speed))
                paragraphs = [speech_text(p) for p in re.split(r"\n\s*\n", self.chapters[idx][1])]
                paragraphs = [p for p in paragraphs if p]
                if paragraphs:
                    jobs.append({"chapter": idx + 1, "paragraphs": paragraphs, "output": str(target)})
                else:
                    errors.append(f"Chapter {idx + 1}: no readable text")
            if jobs and not cancel_event.is_set():
                manifest = self.audio_folder / "_preload_queue.json"
                manifest.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                worker = Path(__file__).with_name("kokoro_paragraph_worker.py")
                command = [sys.executable, str(worker), "batch", "--model", str(self.model_dir / "kokoro-v1.0.onnx"), "--voices", str(self.model_dir / "voices-v1.0.bin"), "--voice", voice_id, "--speed", str(speed), "--cache-identity", self._renderer_cache_identity(), "--manifest", str(manifest)]
                completed = set()
                successful = set()
                batch_summary = None
                try:
                    process = subprocess.Popen(command, cwd=self.model_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=KOKORO_WORKER_PRIORITY)
                    self.preload_process = process
                    stderr_lines = []
                    stderr_thread = threading.Thread(target=_drain_text_stream, args=(process.stderr, stderr_lines), daemon=True)
                    stderr_thread.start()
                    if cancel_event.is_set() and process.poll() is None:
                        process.terminate()
                    assert process.stdout is not None
                    expected_job_id = None
                    for output_line in process.stdout:
                        if cancel_event.is_set() and process.poll() is None:
                            process.terminate()
                        line = output_line.strip()
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                errors.append(f"Malformed worker event: {line}")
                                continue
                            if event.get("protocol") != 1 or not isinstance(event.get("job_id"), str):
                                errors.append(f"Unsupported or malformed worker event: {line}")
                                continue
                            if expected_job_id is None:
                                expected_job_id = event["job_id"]
                            elif event["job_id"] != expected_job_id:
                                errors.append(f"Worker event used an unexpected job ID: {line}")
                                continue
                            if event.get("event") == "worker_error":
                                errors.append(str(event.get("error", "Worker failed")))
                                continue
                            if event.get("event") == "batch_summary":
                                expected_chapters = {int(job["chapter"]) for job in jobs}
                                if not _valid_batch_summary(event, expected_chapters,
                                                            {index + 1 for index in successful}):
                                    errors.append("Worker sent an invalid terminal batch summary")
                                else:
                                    batch_summary = event
                                    if event["failed"]:
                                        errors.append(f"Worker reported failed chapters: {event['failed']}")
                                continue
                            chapter = int(event["chapter"]) - 1
                            if event["event"] == "chapter_start":
                                chapter_started_at[chapter + 1] = time.monotonic()
                                self._schedule_after(0, lambda idx=chapter: self._preload_chapter_started(idx))
                            elif event["event"] == "batch_start":
                                self._schedule_after(0, lambda idx=chapter, p=event: self._update_prefetch_batch_progress(idx, p["paragraph"], p["paragraph_end"], p["total"]))
                            elif event["event"] == "paragraph":
                                self._schedule_after(0, lambda idx=chapter, p=event: self._update_prefetch_progress(idx, p["paragraph"], p["total"]))
                            elif event["event"] == "chapter_done":
                                completed.add(chapter)
                                successful.add(chapter)
                                elapsed = finish_timing(chapter + 1, "complete")
                                self._schedule_after(0, lambda idx=chapter, duration=elapsed:
                                                     self._preload_chapter_done(idx, None, duration))
                            elif event["event"] == "paragraph_error":
                                errors.append(f"Chapter {chapter + 1}, paragraph {event.get('paragraph', '?')}: {event.get('error', 'Kokoro could not render this paragraph')}")
                            elif event["event"] == "chapter_error":
                                completed.add(chapter)
                                elapsed = finish_timing(chapter + 1, "failed")
                                errors.append(f"Chapter {chapter + 1}: {event.get('error', 'Kokoro could not render this chapter')}")
                                self._schedule_after(0, lambda idx=chapter, duration=elapsed:
                                                     self._preload_chapter_done(idx, "generation failed", duration))
                        except (ValueError, KeyError, TypeError):
                            if line:
                                errors.append(line)
                    code = process.wait()
                    stderr_thread.join(timeout=2)
                    if self.preload_process is process:
                        self.preload_process = None
                    if code != 0 and not cancel_event.is_set():
                        errors.append(f"Background Kokoro worker exited with code {code}: " + "\n".join(stderr_lines[-5:]))
                    if not cancel_event.is_set():
                        if batch_summary is None:
                            errors.append("Background Kokoro worker exited without a terminal batch summary")
                        for job in jobs:
                            chapter = job["chapter"] - 1
                            if chapter not in completed:
                                self._schedule_after(0, lambda idx=chapter: self._preload_chapter_done(idx, "not completed"))
                except Exception as exc:
                    if not cancel_event.is_set():
                        errors.append(str(exc))
                finally:
                    manifest.unlink(missing_ok=True)

        def work():
            try:
                run_preload()
            except Exception as exc:
                if not cancel_event.is_set():
                    errors.append(f"Preload failed: {exc}")
            finally:
                cancelled = cancel_event.is_set()
                try:
                    self._schedule_after(
                        0,
                        lambda errs=list(errors), was_cancelled=cancelled:
                            self._preload_all_done(errs, was_cancelled),
                    )
                except tk.TclError:
                    pass

        try:
            self._start_daemon(work)
        except RuntimeError as exc:
            self.preloading = False
            self.preload_state = "failed"
            self.preload_cancel_event = None
            self.preload_stop_btn.configure(state="disabled")
            self.prefetch_info.configure(text=f"Could not start background preload: {exc}")

    def stop_preload(self):
        if not self.preloading or self.preload_cancel_event is None:
            return
        self.preload_restart_requested = False
        self.preload_state = "cancelled"
        self.preload_cancel_event.set()
        process = self.preload_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.preload_stop_btn.configure(state="disabled")
        self.prefetch_info.configure(text="Stopping preload… completed chapter files are being kept")

    def _render_background_chapter(self, idx: int, target: Path, speed: float, voice_id: str) -> str | None:
        input_file = target.with_suffix(".input.json")
        paragraphs = [speech_text(p) for p in re.split(r"\n\s*\n", self.chapters[idx][1])]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            return "no readable text"
        input_file.write_text(json.dumps(paragraphs, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        worker = Path(__file__).with_name("kokoro_paragraph_worker.py")
        command = [sys.executable, str(worker), "single", "--model", str(self.model_dir / "kokoro-v1.0.onnx"),
                   "--voices", str(self.model_dir / "voices-v1.0.bin"), "--voice", voice_id,
                   "--speed", str(speed), "--cache-identity", self._renderer_cache_identity(),
                   "--chapter", str(idx + 1),
                   "--input", str(input_file), "--output", str(target)]
        try:
            process = subprocess.Popen(command, cwd=self.model_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=KOKORO_WORKER_PRIORITY)
            stderr_lines = []
            stderr_thread = threading.Thread(target=_drain_text_stream, args=(process.stderr, stderr_lines), daemon=True)
            stderr_thread.start()
            assert process.stdout is not None
            recent = []
            for output_line in process.stdout:
                line = output_line.strip()
                if not line:
                    continue
                recent.append(line)
                recent = recent[-3:]
                try:
                    progress = json.loads(line)
                    self._schedule_after(0, lambda chapter=idx, p=progress: self._update_prefetch_progress(chapter, p["paragraph"], p["total"]))
                except (ValueError, KeyError):
                    pass
            code = process.wait()
            stderr_thread.join(timeout=2)
            if code != 0 or any("Error:" in item or "Traceback" in item for item in recent):
                return "\n".join(recent + stderr_lines[-3:]) or f"Kokoro exited with code {code}"
            input_file.unlink(missing_ok=True)
        except Exception as exc:
            return str(exc)
        return None

    def _preload_chapter_started(self, chapter_idx: int):
        self.prefetching = {chapter_idx}
        self.preload_active_started_at = time.monotonic()
        self.prefetch_bar.configure(maximum=max(1, len(self.chapters)), value=len(self.prefetch_ready))
        previous = f"{self.preload_last_timing_text} • " if self.preload_last_timing_text else ""
        self.prefetch_info.configure(
            text=f"{previous}Preparing Chapter {chapter_idx + 1} of {len(self.chapters)}… • elapsed 0s")
        self._refresh_chapter_list()

    def _preload_queue_started(self, first_chapter: int, total: int):
        self.prefetch_info.configure(text=f"Continuing from Chapter {first_chapter} of {total}; earlier missing chapters will be filled after the end")

    def _update_prefetch_progress(self, chapter_idx: int, current: int, total: int):
        percent = round(current * 100 / max(total, 1))
        completed = len(self.prefetch_ready)
        self.prefetch_bar.configure(maximum=max(1, len(self.chapters)), value=completed + current / max(1, total))
        started_at = getattr(self, "preload_active_started_at", None)
        elapsed = _format_elapsed(time.monotonic() - started_at) if started_at is not None else "0s"
        previous = f"{getattr(self, 'preload_last_timing_text', '')} • " if getattr(self, "preload_last_timing_text", "") else ""
        self.prefetch_info.configure(text=f"{previous}Ch. {chapter_idx + 1}/{len(self.chapters)} • paragraph {current}/{total} ({percent}%) • elapsed {elapsed} • {completed} ready")
        self._update_voice_status()

    def _update_prefetch_batch_progress(self, chapter_idx: int, first: int, last: int, total: int):
        completed = len(self.prefetch_ready)
        started_at = getattr(self, "preload_active_started_at", None)
        elapsed = _format_elapsed(time.monotonic() - started_at) if started_at is not None else "0s"
        previous = f"{getattr(self, 'preload_last_timing_text', '')} • " if getattr(self, "preload_last_timing_text", "") else ""
        self.prefetch_info.configure(text=f"{previous}Ch. {chapter_idx + 1}/{len(self.chapters)} • preparing paragraphs {first}–{last}/{total} • elapsed {elapsed} • {completed} ready")

    def _preload_chapter_done(self, chapter_idx: int, error: str | None,
                              elapsed_seconds: float | None = None, cached: bool = False):
        self.prefetching.discard(chapter_idx)
        self.preload_active_started_at = None
        if error is None:
            self.prefetch_ready.add(chapter_idx)
            if self.prefetch_voice_id:
                ready_chapters = self.audio_cache_chapters.setdefault(self.prefetch_voice_id, set())
                ready_chapters.add(chapter_idx)
                self.audio_cache_counts[self.prefetch_voice_id] = len(ready_chapters)
            self._update_voice_status()
        self.prefetch_bar.configure(maximum=max(1, len(self.chapters)), value=len(self.prefetch_ready))
        if cached:
            status = f"Chapter {chapter_idx + 1} already cached • {len(self.prefetch_ready)} chapters ready"
        elif elapsed_seconds is not None:
            elapsed_text = _format_elapsed(elapsed_seconds)
            if error is None:
                self.preload_last_timing_text = f"Last: Ch. {chapter_idx + 1} {elapsed_text}"
                status = f"Chapter {chapter_idx + 1} ready in {elapsed_text} • {len(self.prefetch_ready)} chapters ready"
            else:
                self.preload_last_timing_text = f"Last: Ch. {chapter_idx + 1} failed after {elapsed_text}"
                status = f"Chapter {chapter_idx + 1} failed after {elapsed_text} • {len(self.prefetch_ready)} chapters ready"
        else:
            status = f"Preparing novel audio • {len(self.prefetch_ready)} of {len(self.chapters)} chapters ready"
        self.prefetch_info.configure(text=status)
        self._update_voice_status()
        self._refresh_chapter_list()

    def _preload_all_done(self, errors: list[str], cancelled: bool = False):
        if self._closing:
            self.preloading = False
            self.preload_state = "cancelled"
            self.prefetching.clear()
            self.preload_process = None
            self.preload_cancel_event = None
            self.preload_restart_requested = False
            return
        restart = self.preload_restart_requested
        self.preloading = False
        self.preload_state = "cancelled" if cancelled else ("failed" if errors else "complete")
        self.preload_stop_btn.configure(state="disabled")
        self.prefetching.clear()
        self.preload_process = None
        self.preload_cancel_event = None
        self.preload_restart_requested = False
        if cancelled:
            self._refresh_chapter_list()
            if restart:
                self.prefetch_info.configure(text=f"Restarting preload with {self.voice_label.get()}… saved chapter files will be reused")
                self._schedule_after(100, self._start_novel_preload)
            else:
                self.prefetch_info.configure(text="Background preload stopped; completed audio files were kept")
            self._update_voice_status()
            return
        if errors:
            self.prefetch_info.configure(text=f"Preload finished with {len(errors)} chapter error(s). See error details.")
            messagebox.showwarning("Some chapters need attention", "Preloading reached the end, but some chapters failed:\n\n" + "\n\n".join(errors[:8]))
        else:
            self.prefetch_info.configure(text=f"All {len(self.chapters)} chapter audio files are ready.")
            self.prefetch_bar.configure(value=len(self.chapters))
        self._update_voice_status()
        self._refresh_chapter_list()
        self._refresh_audio_cache_counts()

    def _refresh_chapter_list(self):
        if not self.chapters:
            return
        y_position = self.chapter_list.yview()[0] if self.chapter_list.size() else 0.0
        if len(self.chapter_list_labels) != len(self.chapters) or self.chapter_list.size() != len(self.chapters):
            self.chapter_list.delete(0, tk.END)
            self.chapter_list_labels = [""] * len(self.chapters)
        for idx, (title, _body) in enumerate(self.chapters):
            marker = "▶  " if idx == self.chapter_idx else ""
            if idx in self.prefetching:
                marker = "⏳  "
            elif idx in self.prefetch_ready:
                marker = "✓  "
            label = marker + title[:50]
            if self.chapter_list_labels[idx] != label:
                if idx < self.chapter_list.size():
                    self.chapter_list.delete(idx)
                    self.chapter_list.insert(idx, label)
                else:
                    self.chapter_list.insert(tk.END, label)
                self.chapter_list_labels[idx] = label
        self.chapter_list.selection_clear(0, tk.END)
        self.chapter_list.selection_set(self.chapter_idx)
        self.chapter_list.yview_moveto(y_position)

    def _play_from(self, offset: float):
        if self.audio is None:
            return
        if sd is None:
            messagebox.showerror("Audio playback unavailable", "sounddevice could not be imported. Install it in the Python environment running this reader.")
            return
        start = min(len(self.audio), max(0, int(offset * self.samplerate)))
        try:
            sd.stop()
            sd.play(self.audio[start:], self.samplerate)
        except sd.PortAudioError as exc:
            self.playing = False
            self.started_at = None
            self.play_btn.configure(text="▶ Play chapter")
            self.status.configure(text="Audio output unavailable")
            messagebox.showerror(
                "Audio output unavailable",
                "Windows could not open the selected audio output device.\n\n"
                "Check that speakers/headphones are connected, then choose another "
                "default output device in Windows and try Play again.\n\n"
                f"PortAudio: {exc}",
            )
            return
        self.offset = offset
        self.started_at = time.monotonic()
        self.playing = True
        self.play_btn.configure(text="Ⅱ Pause")
        self.status.configure(text=f"Playing chapter {self.chapter_idx + 1}")

    def pause_audio(self):
        if self.playing and self.started_at is not None:
            self.offset += time.monotonic() - self.started_at
            if sd is not None:
                sd.stop()
            self.playing = False
            self.started_at = None
            self.play_btn.configure(text="▶ Resume")
            self.status.configure(text="Paused")
            self._save_progress()

    def stop_audio(self):
        if sd is not None:
            sd.stop()
        self.playing = False
        self.started_at = None
        old_audio = self.audio
        self.audio = None
        _release_audio_buffer(old_audio)
        self.loaded_audio_voice_id = None
        self._update_voice_status()
        self.offset = 0.0
        self.play_btn.configure(text="▶ Play chapter")

    def step_chapter(self, delta: int):
        if not self.chapters:
            return
        target = self.chapter_idx + delta
        if 0 <= target < len(self.chapters):
            self.stop_audio()
            self.chapter_idx = target
            self.show_chapter()
            self.resume_offset = 0.0
            self._save_progress(offset=0.0)

    def click_paragraph(self, event):
        if not self.lines or self.audio is None:
            return
        index = self.text.index(f"@{event.x},{event.y}")
        line_no = int(index.split(".")[0]) - 2
        line_no = max(0, min(len(self.lines) - 1, line_no // 2))
        if self.paragraph_timings:
            speakable_idx = sum(1 for i in self.timing_line_map if i < line_no)
            timing = self.paragraph_timings[min(speakable_idx, len(self.paragraph_timings) - 1)]
            self._play_from(float(timing["start"]))
        else:
            weights = [max(1, len(re.findall(r"\S+", line))) for line in self.lines]
            self._play_from(self.duration * sum(weights[:line_no]) / max(1, sum(weights)))

    def _tick(self):
        if self.playing and self.started_at is not None and self.audio is not None:
            elapsed = self.offset + (time.monotonic() - self.started_at)
            if time.monotonic() - getattr(self, "last_progress_save", 0.0) >= 5:
                self._save_progress(offset=elapsed)
                self.last_progress_save = time.monotonic()
            if elapsed >= self.duration:
                self.playing = False
                self.started_at = None
                self.offset = self.duration
                self.play_btn.configure(text="▶ Replay chapter")
                if self.auto_next.get() and self.chapter_idx + 1 < len(self.chapters):
                    self.status.configure(text="Chapter finished — loading next…")
                    self.prefetch_ready.discard(self.chapter_idx + 1)
                    self.chapter_idx += 1
                    self.offset = 0.0
                    self.show_chapter()
                    self._save_progress(offset=0.0)
                    self._schedule_after(350, self._generate_and_play)
                else:
                    self.status.configure(text="Novel finished" if self.chapter_idx + 1 == len(self.chapters) else "Chapter finished")
            else:
                self.progress["value"] = 100 * elapsed / max(self.duration, 0.001)
                if self.paragraph_timings:
                    timing_idx = self._timing_index_at(elapsed)
                    line_index = self.timing_line_map[min(timing_idx, len(self.timing_line_map) - 1)] if self.timing_line_map else 0
                else:
                    weights = [max(1, len(re.findall(r"\S+", line))) for line in self.lines]
                    total = sum(weights) or 1
                    threshold = elapsed / max(self.duration, 0.001) * total
                    running = 0
                    line_index = 0
                    for i, weight in enumerate(weights):
                        running += weight
                        if threshold < running:
                            line_index = i
                            break
                self._highlight(line_index, elapsed)
        self._schedule_after(150, self._tick)

    def _timing_index_at(self, elapsed: float) -> int:
        """Keep the nearest spoken paragraph highlighted during inserted pauses."""
        if not self._valid_timing_indices:
            return 0
        position = bisect.bisect_right(self._valid_timing_starts, elapsed) - 1
        if position < 0:
            return self._valid_timing_indices[0]
        index = self._valid_timing_indices[position]
        timing = self.paragraph_timings[index]
        end = float(timing.get("end", self._valid_timing_starts[position]))
        if elapsed < end:
            return index
        return index  # During a pause, keep the last spoken paragraph highlighted.

    def _rebuild_timing_lookup(self) -> None:
        """Cache valid, ordered interval starts so playback ticks use binary search."""
        intervals = []
        for index, timing in enumerate(self.paragraph_timings):
            start = float(timing.get("start", 0.0))
            end = float(timing.get("end", start))
            if end > start:  # Failed or empty paragraph: no audio interval.
                intervals.append((start, index))
        intervals.sort()
        self._valid_timing_starts = [start for start, _ in intervals]
        self._valid_timing_indices = [index for _, index in intervals]

    def _load_word_timings(self, audio_path: Path, timing_path: Path,
                           audio_hash: str | None = None) -> list[list[dict]]:
        """Load a Vosk timing sidecar only when it belongs to this WAV/text cache."""
        sidecar = Path(str(audio_path) + ".words.json")
        if not sidecar.is_file() or not timing_path.is_file():
            return [[] for _ in self.lines]
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return [[] for _ in self.lines]
            audio_stat = audio_path.stat()
            timing_hash = hashlib.sha256(timing_path.read_bytes()).hexdigest()
            if (data.get("format") != "kokoro-vosk-word-timings-v1"
                    or data.get("accepted_for_highlighting") is not True
                    or data.get("audio_size") != audio_stat.st_size
                    or data.get("audio_mtime_ns") != audio_stat.st_mtime_ns
                    or data.get("timing_sha256") != timing_hash):
                return [[] for _ in self.lines]
            stored_audio_hash = data.get("audio_sha256")
            if stored_audio_hash is not None:
                actual_audio_hash = audio_hash or _sha256_path(audio_path)
                if stored_audio_hash != actual_audio_hash:
                    return [[] for _ in self.lines]

            result: list[list[dict]] = [[] for _ in self.lines]
            word_pattern = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
            stored_paragraphs = data.get("paragraphs", [])
            if (not isinstance(stored_paragraphs, list)
                    or len(stored_paragraphs) != len(self.timing_line_map)):
                return result
            audio_info = sf.info(audio_path)
            duration = audio_info.frames / audio_info.samplerate
            min_confidence = MIN_WORD_HIGHLIGHT_CONFIDENCE
            previous_word_start = previous_word_end = -math.inf
            for timing_index, line_index in enumerate(self.timing_line_map):
                if line_index >= len(self.lines):
                    return [[] for _ in self.lines]
                display_words = list(word_pattern.finditer(self.lines[line_index]))
                paragraph_data = stored_paragraphs[timing_index]
                if (not isinstance(paragraph_data, dict)
                        or paragraph_data.get("paragraph") != timing_index):
                    return [[] for _ in self.lines]
                stored_words = paragraph_data.get("words", [])
                if not isinstance(stored_words, list) or len(display_words) != len(stored_words):
                    return [[] for _ in self.lines]
                if paragraph_data.get("accepted_for_highlighting", True) is not True:
                    continue
                base_line = 3 + line_index * 2
                for match, word in zip(display_words, stored_words):
                    if not isinstance(word, dict) or word.get("match") not in {"exact", "substitution"}:
                        continue
                    try:
                        start_time = float(word["start"])
                        end_time = float(word["end"])
                        confidence = float(word["confidence"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if (not math.isfinite(start_time) or not math.isfinite(end_time)
                            or not math.isfinite(confidence) or start_time < 0
                            or end_time <= start_time or end_time > duration + 0.05
                            or not 0.0 <= confidence <= 1.0):
                        continue
                    if start_time < previous_word_start or end_time < previous_word_end:
                        return [[] for _ in self.lines]
                    previous_word_start, previous_word_end = start_time, end_time
                    if word.get("match") != "exact" or confidence < min_confidence:
                        continue
                    if word_pattern.fullmatch(word.get("text", "")) is None:
                        continue
                    if word["text"].casefold().replace("’", "'") != match.group().casefold().replace("’", "'"):
                        return [[] for _ in self.lines]
                    result[line_index].append({
                        "start": float(start_time),
                        "end": float(end_time),
                        "tk_start": self.text.index(f"{base_line}.0+{match.start()}c"),
                        "tk_end": self.text.index(f"{base_line}.0+{match.end()}c"),
                    })
            return result
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return [[] for _ in self.lines]

    def _highlight(self, index: int, elapsed: float | None = None):
        # title is two lines; text paragraphs each use two display lines in the widget.
        start = f"{3 + index * 2}.0"
        end = f"{4 + index * 2}.0"
        if self._highlight_range != (start, end):
            if self._highlight_range is not None:
                self.text.tag_remove("current", *self._highlight_range)
            self.text.tag_add("current", start, end)
            self._highlight_range = (start, end)

        word_range = None
        if elapsed is not None and index < len(self.word_spans):
            for word in self.word_spans[index]:
                if word["start"] <= elapsed < word["end"]:
                    word_range = (word["tk_start"], word["tk_end"])
                    break
        if word_range != self._highlight_word_range:
            if self._highlight_word_range is not None:
                self.text.tag_remove("current_word", *self._highlight_word_range)
            if word_range is not None:
                self.text.tag_add("current_word", *word_range)
            self._highlight_word_range = word_range
        box = self.text.bbox(start)
        if box is None or box[1] < 0 or box[1] > self.text.winfo_height():
            self.text.see(start)

    def close(self):
        if self._closing:
            return
        if self.playing and self.started_at is not None:
            self._save_progress(offset=self.offset + time.monotonic() - self.started_at)
        else:
            self._save_progress()
        self._closing = True
        self._audio_load_generation += 1
        self.cache_scan_generation += 1
        self.preload_restart_requested = False
        if self.preloading:
            self.preload_state = "cancelled"
        if self.preload_cancel_event is not None:
            self.preload_cancel_event.set()
        for process in (self.generation_process, self.preload_process):
            _terminate_process_bounded(process, timeout=2)
        with self._background_threads_lock:
            background_threads = tuple(self._background_threads)
        for thread in background_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self.preloading = False
        self.preload_process = None
        self.preload_cancel_event = None
        self._cancel_phone_screen_off()
        self.phone_audio_server.stop()
        self.stop_audio()
        self.root.destroy()

    def _schedule_after(self, delay_ms: int, callback, *args):
        """Ignore worker/UI callbacks after shutdown has begun."""
        if self._closing:
            return None
        try:
            return self.root.after(delay_ms, callback, *args)
        except tk.TclError:
            return None

    def _start_daemon(self, target) -> threading.Thread:
        def run():
            current = threading.current_thread()
            try:
                target()
            finally:
                with self._background_threads_lock:
                    self._background_threads.discard(current)

        thread = threading.Thread(target=run, daemon=True)
        with self._background_threads_lock:
            if self._closing:
                return thread
            self._background_threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with self._background_threads_lock:
                self._background_threads.discard(thread)
            raise
        return thread


if __name__ == "__main__":
    root = tk.Tk()
    NovelReader(root)
    root.mainloop()
