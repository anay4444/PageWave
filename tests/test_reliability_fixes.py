import contextlib
import http.client
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kokoro_novel_reader as reader
import kokoro_paragraph_worker as worker
import vosk_align_chapter as aligner


class FakeKokoro:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def create(self, *_args, **_kwargs):
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


class DummyWidget:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class DummyText:
    def index(self, value):
        return value


class ReliabilityFixTests(unittest.TestCase):
    def test_diagnostics_mitigation_and_open_item_count_matches_audit(self):
        diagnostics = (ROOT / "Kokoro_Script_Diagnostics.md").read_text(encoding="utf-8")
        tracker_match = re.search(
            r"## Improvement progress tracker\s*(.*?)(?=\n## )", diagnostics, flags=re.DOTALL
        )
        self.assertIsNotNone(tracker_match)
        tracker = tracker_match.group(1)
        rows = re.findall(
            r"^\|\s*(\d+)\s*\|\s*([^|\n]+)\|\s*([^|\n]+)\|\s*$",
            tracker,
            flags=re.MULTILINE,
        )
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 14)))
        self.assertTrue(all(row[1].strip() and row[2].strip() for row in rows))
        self.assertRegex(diagnostics, r"13/13 findings have at least one code mitigation")
        self.assertRegex(diagnostics, r"0/13 are fully verified end to end")

        audit = (ROOT / "Kokoro_Fix_Audit.md").read_text(encoding="utf-8")
        followups = re.findall(r"^\*\*Task (\d+) follow-up[^\n]*", audit, flags=re.MULTILINE)
        self.assertEqual([int(task) for task in followups], list(range(1, 8)))
        self.assertTrue(all(re.search(r"\btests?\b|\bsmoke\b|\bcoverage\b", line, re.IGNORECASE)
                            for line in re.findall(r"^\*\*Task \d+ follow-up[^\n]*", audit, flags=re.MULTILINE)))

    def test_batch_summary_requires_consistent_terminal_accounting(self):
        expected = {1, 2, 3}
        successful = {1, 2}
        summary = {"status": "partial_failure", "total": 3,
                   "succeeded": [1, 2], "failed": [3]}
        self.assertTrue(reader._valid_batch_summary(summary, expected, successful))
        summary["failed"] = []
        self.assertFalse(reader._valid_batch_summary(summary, expected, successful))

    def test_playback_audio_loader_decodes_in_chunks_to_disk_backed_array(self):
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "sample.wav"
            original = np.column_stack((np.linspace(-0.8, 0.8, 901), np.linspace(0.7, -0.7, 901))).astype(np.float32)
            sf.write(audio_path, original, 24000, subtype="PCM_16")
            with mock.patch.object(reader, "AUDIO_DECODE_CHUNK_FRAMES", 64), \
                    mock.patch.object(reader, "AUDIO_IN_MEMORY_LIMIT_BYTES", 0):
                audio, rate, audio_hash = reader._read_audio_for_playback(audio_path)
            mapped_path = Path(audio.filename)
            self.assertIsInstance(audio, np.memmap)
            self.assertEqual(audio.shape, original.shape)
            self.assertLessEqual(reader.AUDIO_DECODE_CHUNK_FRAMES * 2 * 4, 2 * 1024 * 1024)
            self.assertTrue(np.allclose(audio, sf.read(audio_path, dtype="float32", always_2d=True)[0]))
            self.assertTrue(mapped_path.is_file())
            reader._release_audio_buffer(audio)
            self.assertFalse(mapped_path.exists())
            self.assertEqual(rate, 24000)
            self.assertEqual(audio_hash, hashlib.sha256(audio_path.read_bytes()).hexdigest())

    def test_small_playback_audio_uses_memory_without_temporary_disk_file(self):
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "small.wav"
            original = np.column_stack((np.linspace(-0.5, 0.5, 128),)).astype(np.float32)
            sf.write(audio_path, original, 24000, subtype="PCM_16")
            with mock.patch.object(reader, "AUDIO_IN_MEMORY_LIMIT_BYTES", 1024):
                audio, rate, _audio_hash = reader._read_audio_for_playback(audio_path)
            self.assertFalse(isinstance(audio, np.memmap))
            self.assertEqual(audio.shape, original.shape)
            self.assertTrue(np.allclose(audio, sf.read(audio_path, dtype="float32", always_2d=True)[0]))
            self.assertEqual(rate, 24000)

    def test_playback_seeks_with_a_view_of_the_disk_backed_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            mapped_path = Path(folder) / "audio.f32"
            audio = np.memmap(mapped_path, dtype=np.float32, mode="w+", shape=(100, 2))
            audio[:] = np.arange(200, dtype=np.float32).reshape(100, 2)
            played = {}
            fake_device = SimpleNamespace(
                stop=lambda: None,
                play=lambda data, samplerate: played.update(data=data, samplerate=samplerate),
            )
            app = SimpleNamespace(
                audio=audio, samplerate=20, duration=5.0, offset=0.0,
                started_at=None, playing=False, chapter_idx=0,
                play_btn=DummyWidget(), status=DummyWidget(),
            )
            with mock.patch.object(reader, "sd", fake_device):
                reader.NovelReader._play_from(app, 1.5)
            self.assertIsInstance(played["data"], np.memmap)
            self.assertTrue(np.array_equal(played["data"], audio[30:]))
            self.assertEqual(played["samplerate"], 20)
            audio._mmap.close()

    def test_playback_reports_unavailable_portaudio_output_without_crashing(self):
        class PortAudioError(Exception):
            pass

        portaudio_error = PortAudioError

        class FailedDevice:
            PortAudioError = portaudio_error

            @staticmethod
            def stop():
                pass

            @staticmethod
            def play(_data, _samplerate):
                raise PortAudioError("MME error 1")

        app = SimpleNamespace(
            audio=np.zeros((100, 1), dtype=np.float32), samplerate=20,
            duration=5.0, offset=0.0, started_at=None, playing=False,
            play_btn=DummyWidget(), status=DummyWidget(),
        )
        with mock.patch.object(reader, "sd", FailedDevice), \
             mock.patch.object(reader.messagebox, "showerror") as showerror:
            reader.NovelReader._play_from(app, 0.0)
        self.assertFalse(app.playing)
        self.assertEqual(app.status.options["text"], "Audio output unavailable")
        showerror.assert_called_once()

    def test_phone_audio_server_serves_private_player_and_wav(self):
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "chapter.wav"
            next_path = Path(folder) / "next.wav"
            third_path = Path(folder) / "third.wav"
            audio_path.write_bytes(b"RIFF-test-audio")
            next_path.write_bytes(b"RIFF-next-audio")
            third_path.write_bytes(b"RIFF-third-audio")
            def phone_state(chapter=None):
                if chapter == 4:
                    return {
                        "chapter": 4, "audio_path": next_path, "next_path": third_path,
                        "next_title": "Chapter 6", "auto_next": True,
                    }
                return {
                    "chapter": 3, "audio_path": audio_path, "next_path": next_path,
                    "next_title": "Chapter 5", "auto_next": True,
                }
            server = reader.PhoneAudioServer(
                lambda: audio_path,
                lambda: "Chapter 4",
                phone_state,
            )
            try:
                url = server.start()
                parsed = urllib.parse.urlsplit(url)
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                connection.request("GET", parsed.path + "?" + parsed.query)
                page = connection.getresponse()
                self.assertEqual(page.status, 200)
                self.assertIn(b"Chapter 4", page.read())
                connection.close()
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                connection.request("GET", "/audio?" + parsed.query)
                audio = connection.getresponse()
                self.assertEqual(audio.status, 200)
                self.assertEqual(audio.read(), b"RIFF-test-audio")
                connection.close()
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                connection.request("GET", "/state?" + parsed.query)
                state = json.loads(connection.getresponse().read())
                self.assertTrue(state["auto_next"])
                self.assertTrue(state["next_available"])
                self.assertIn("chapter=5", state["next_url"])
                connection.close()
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                connection.request("GET", "/audio?" + parsed.query + "&chapter=5")
                next_audio = connection.getresponse()
                self.assertEqual(next_audio.status, 200)
                self.assertEqual(next_audio.read(), b"RIFF-next-audio")
                connection.close()
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                connection.request("GET", "/state?" + parsed.query + "&chapter=5")
                second_state = json.loads(connection.getresponse().read())
                self.assertIn("chapter=6", second_state["next_url"])
                connection.close()
            finally:
                server.stop()

    def test_phone_state_can_serve_next_legacy_cache_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio_folder = root / "audio"
            audio_folder.mkdir()
            next_path = audio_folder / "chapter_0002_old-hash.wav"
            next_path.write_bytes(b"RIFF-next-audio")
            app = SimpleNamespace(
                chapter_idx=0, chapters=[("One", "one"), ("Two", "two")],
                loaded_audio_voice_id="af_sarah", current_audio_path=None,
                phone_audio_speed="1.0", speed=SimpleNamespace(get=lambda: "1.0"),
                phone_auto_next_enabled=True,
                audio_folder=audio_folder, output_root=root, novel_path=root / "book.txt",
                audio_cache_chapters={"af_sarah": {0, 1}},
                _audio_search_folders=lambda: [audio_folder],
                _find_cached_audio=lambda *_args: None,
                _legacy_audio_is_usable=lambda path: path == next_path,
                _normalized_speed=lambda: "1.0",
            )
            app._find_phone_cached_audio = lambda chapter, voice: reader.NovelReader._find_phone_cached_audio(app, chapter, voice)
            state = reader.NovelReader._phone_audio_state(app)
            self.assertEqual(state["next_path"], next_path)
            self.assertTrue(state["auto_next"])

    def test_generated_chapter_playback_honors_saved_resume_offset(self):
        played_offsets = []
        preload_calls = []
        audio = np.zeros((1000, 1), dtype=np.float32)
        app = SimpleNamespace(
            _closing=False, _audio_load_generation=1, play_btn=DummyWidget(), audio=None,
            samplerate=0, loaded_audio_voice_id=None, voice_label=SimpleNamespace(get=lambda: "US Female"),
            resume_offset=0.0, duration=0.0, paragraph_timings=[], word_spans=[],
            audio_cache_chapters={}, audio_cache_counts={}, chapter_idx=0,
            prefetch_voice_id=None, prefetch_speed=None, prefetch_ready=set(),
            _update_voice_status=lambda: None, _rebuild_timing_lookup=lambda: None,
            _load_word_timings=lambda *_args, **_kwargs: [], _normalized_speed=lambda: "1.0",
            _play_from=lambda offset: played_offsets.append(offset),
            _start_prefetch_next=lambda: preload_calls.append(True),
        )
        with mock.patch.object(reader, "_cache_audio_digest_matches", return_value=True):
            reader.NovelReader._finish_audio_load(
                app, 1, Path("generated.wav"), audio, 100, "digest", True,
                "af_nicole", None, 7.8,
            )
        self.assertEqual(app.offset, 7.8)
        self.assertEqual(played_offsets, [7.8])
        self.assertEqual(preload_calls, [True])

    def test_preload_order_starts_at_selected_chapter_then_wraps(self):
        self.assertEqual(reader._preload_order([0, 1, 3, 4, 6], 3), [3, 4, 6, 0, 1])

    def test_preload_window_is_bounded_to_nearby_chapters(self):
        self.assertEqual(list(reader._preload_window(266, 90, 3)), [90, 91, 92, 93])
        self.assertEqual(list(reader._preload_window(266, 264, 3)), [264, 265])

    def test_preload_elapsed_formatter_and_append_only_timing_log(self):
        self.assertEqual(reader._format_elapsed(421), "7m 01s")
        self.assertEqual(reader._format_elapsed(3661), "1h 01m 01s")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "_preload_timings.jsonl"
            reader._append_preload_timing(path, 79, 421.23456, "complete", "af_sarah", "1.0")
            reader._append_preload_timing(path, 80, 12.5, "failed", "af_sarah", "1.0")
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["chapter"] for record in records], [79, 80])
        self.assertEqual([record["result"] for record in records], ["complete", "failed"])
        self.assertEqual(records[0]["elapsed_seconds"], 421.235)
        self.assertEqual(records[0]["voice"], "af_sarah")

    def test_preload_status_shows_live_and_completed_chapter_duration(self):
        app = SimpleNamespace(
            chapters=[("title", "text") for _ in range(80)], chapter_idx=78,
            prefetching=set(), prefetch_ready=set(), prefetch_bar=DummyWidget(),
            prefetch_info=DummyWidget(), preload_last_timing_text="",
            preload_active_started_at=None, prefetch_voice_id="af_sarah",
            audio_cache_chapters={}, audio_cache_counts={},
            _update_voice_status=lambda: None, _refresh_chapter_list=lambda: None,
        )
        with mock.patch.object(reader.time, "monotonic", side_effect=[100.0, 160.0]):
            reader.NovelReader._preload_chapter_started(app, 78)
            reader.NovelReader._update_prefetch_progress(app, 78, 3, 50)
        self.assertIn("elapsed 1m 00s", app.prefetch_info.options["text"])
        reader.NovelReader._preload_chapter_done(app, 78, None, elapsed_seconds=421.0)
        self.assertIn("Chapter 79 ready in 7m 01s", app.prefetch_info.options["text"])

    def test_chapter_selection_does_not_cancel_or_restart_active_preload(self):
        cancelled = threading.Event()
        process = SimpleNamespace(poll=lambda: None, terminate=mock.Mock())
        app = SimpleNamespace(
            chapters=[("Chapter", "text")], model_dir=Path("model"),
            novel_path=Path("novel.txt"), preloading=True,
            preload_restart_requested=False, preload_cancel_event=cancelled,
            preload_process=process, chapter_idx=80, prefetching={80},
            prefetch_info=DummyWidget(),
        )
        reader.NovelReader._restart_preload_from_selected_chapter(app)
        self.assertFalse(app.preload_restart_requested)
        self.assertFalse(cancelled.is_set())
        process.terminate.assert_not_called()
        self.assertIn("continues with Chapter 81", app.prefetch_info.options["text"])

    def test_speech_text_omits_punctuation_only_paragraphs(self):
        self.assertEqual(reader.speech_text("... — ♡"), "")
        self.assertEqual(reader.speech_text("Hello..."), "Hello...")

    def test_saved_directory_status_distinguishes_file_missing_and_creatable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file_path = root / "not-a-folder"
            file_path.write_text("file", encoding="utf-8")
            self.assertEqual(reader._directory_status(file_path), "is a file, not a folder")
            self.assertEqual(reader._directory_status(root / "missing-model"), "does not exist")
            self.assertIsNone(reader._directory_status(root, require_write=True))
            self.assertIsNone(reader._directory_status(root / "new-output", allow_missing=True))
            self.assertFalse(any(item.name.startswith(".kokoro-write-check-") for item in root.iterdir()))
            with mock.patch.object(reader, "_directory_write_probe", return_value=False):
                self.assertEqual(reader._directory_status(root), "is not writable")
                self.assertIsNone(reader._directory_status(root, require_write=False))
                self.assertEqual(
                    reader._directory_status(root / "new-output", allow_missing=True),
                    "cannot be created under the current parent folder",
                )
            with mock.patch.object(reader.os, "scandir", side_effect=PermissionError("read denied")):
                self.assertEqual(reader._directory_status(root, require_write=False), "is not readable")

    def test_audio_folder_selection_does_not_save_unwritable_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_output = root / "old-audio"
            old_output.mkdir()
            rejected = root / "offline-drive"
            app = SimpleNamespace(
                output_root=old_output, novel_path=None, status=DummyWidget(), settings={},
            )
            with mock.patch.object(reader.filedialog, "askdirectory", return_value=str(rejected)), \
                    mock.patch.object(reader, "_directory_status", return_value="cannot be created under the current parent folder"):
                reader.NovelReader.choose_audio_folder(app)
            self.assertEqual(app.output_root, old_output)
            self.assertNotIn("output_root", app.settings)
            self.assertIn("Audio folder unavailable", app.status.options["text"])

    def test_registered_background_thread_is_removed_when_finished(self):
        completed = threading.Event()
        app = SimpleNamespace(_closing=False, _background_threads=set(),
                              _background_threads_lock=threading.Lock())
        thread = reader.NovelReader._start_daemon(app, completed.set)
        thread.join(timeout=2)
        self.assertTrue(completed.is_set())
        self.assertFalse(thread.is_alive())
        self.assertNotIn(thread, app._background_threads)

    def test_timing_lookup_binary_search_preserves_pause_highlight(self):
        instance = reader.NovelReader.__new__(reader.NovelReader)
        instance.paragraph_timings = [
            {"start": 0.0, "end": 1.0},
            {"start": 1.0, "end": 1.0},  # Empty paragraph has no interval.
            {"start": 1.5, "end": 2.0},
        ]
        instance._rebuild_timing_lookup()
        self.assertEqual(instance._timing_index_at(-0.1), 0)
        self.assertEqual(instance._timing_index_at(0.5), 0)
        self.assertEqual(instance._timing_index_at(1.25), 0)  # Inserted pause.
        self.assertEqual(instance._timing_index_at(1.75), 2)
        self.assertEqual(instance._timing_index_at(3.0), 2)

    def test_worker_writes_valid_wav_and_timings(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            fake = FakeKokoro([(np.ones(2400, dtype=np.float32), 24000)])
            with contextlib.redirect_stdout(io.StringIO()):
                worker.render_one(fake, ["A sentence."], output, "af_nicole", 1.0, 1, "job")
            audio, rate = sf.read(output)
            timings = json.loads(Path(str(output) + ".timing.json").read_text(encoding="utf-8"))
            self.assertEqual(rate, 24000)
            self.assertEqual(len(audio), 2400 + int(24000 * 0.28))
            self.assertGreater(timings[0]["end"], timings[0]["start"])
            self.assertTrue(Path(str(output) + ".complete.json").is_file())
            completion = json.loads(Path(str(output) + ".complete.json").read_text(encoding="utf-8"))
            self.assertEqual(completion["renderer_identity"], "direct-render")
            self.assertEqual(completion["audio_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(output))
            Path(str(output) + ".timing.json").write_text("[]", encoding="utf-8")
            self.assertFalse(reader.NovelReader._cached_audio_is_valid(output))

    def test_worker_skips_punctuation_only_paragraph_without_synthesis_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            fake = FakeKokoro([(np.ones(2400, dtype=np.float32), 24000)])
            with contextlib.redirect_stdout(io.StringIO()):
                worker.render_one(fake, ["...", "A sentence."], output,
                                  "af_nicole", 1.0, 78, "job")
            timings = json.loads(Path(str(output) + ".timing.json").read_text(encoding="utf-8"))
            self.assertEqual(len(timings), 1)
            self.assertGreater(timings[0]["end"], timings[0]["start"])

    def test_chapter_78_separator_paragraph_is_filtered_before_inference(self):
        paragraphs = [f"Readable paragraph {index}." for index in range(1, 61)]
        paragraphs.extend(("-" * 118, "If you like the story, please leave a review."))
        with tempfile.TemporaryDirectory() as folder:
            input_path = Path(folder) / "chapter_0078.input.json"
            input_path.write_text(json.dumps(paragraphs), encoding="utf-8")
            readable = worker.load_paragraphs(input_path)
        self.assertEqual(len(paragraphs), 62)
        self.assertEqual(len(readable), 61)
        self.assertEqual(readable[-1], "If you like the story, please leave a review.")
        self.assertFalse(any(set(item) == {"-"} for item in readable))

    def test_worker_failure_event_keeps_actual_chapter_number(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            events = io.StringIO()
            with contextlib.redirect_stdout(events), self.assertRaises(worker.ChapterRenderError):
                worker.render_one(FakeKokoro([RuntimeError("synthesis failed")]),
                                  ["A sentence."], output, "af_nicole", 1.0, 78, "job")
            parsed = [json.loads(line) for line in events.getvalue().splitlines()]
            failure = next(item for item in parsed if item["event"] == "paragraph_error")
            self.assertEqual(failure["chapter"], 78)

    def test_cache_playback_digest_rejects_same_size_mtime_audio_change(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            with contextlib.redirect_stdout(io.StringIO()):
                worker.render_one(FakeKokoro([(np.ones(2400, dtype=np.float32), 24000)]),
                                  ["sample"], output, "af_nicole", 1.0, 1, "job")
            original_stat = output.stat()
            original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertTrue(reader._cache_audio_digest_matches(output, original_hash))
            with output.open("r+b") as stream:
                stream.seek(44)
                stream.write(b"\x00\x00")
            os.utime(output, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            changed_hash = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertNotEqual(changed_hash, original_hash)
            self.assertFalse(reader._cache_audio_digest_matches(output, changed_hash))

    def test_reader_reuses_legacy_voice_speed_text_cache_after_identity_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio_folder = root / "audio"
            audio_folder.mkdir()
            app = SimpleNamespace(
                novel_path=Path("book.txt"), chapters=[("Chapter 1", "Saved sentence.")],
                audio_folder=audio_folder, output_root=root,
                _renderer_cache_identity=lambda: "new-renderer-identity",
                _book_folder_name=lambda: "book",
                _audio_search_folders=lambda: [audio_folder],
                _normalized_speed=lambda: "1.0",
            )
            app._legacy_cache_file = lambda idx, voice, speed: reader.NovelReader._legacy_cache_file(
                app, idx, voice, speed
            )
            app._cache_names = lambda idx, voice, speed: reader.NovelReader._cache_names(
                app, idx, voice, speed
            )
            app._cached_audio_is_valid = reader.NovelReader._cached_audio_is_valid
            app._create_estimated_timings = lambda *_args: None
            legacy_audio = app._legacy_cache_file(0, "af_sarah", "1.0")
            sf.write(legacy_audio, np.ones(24000, dtype=np.float32), 24000)
            Path(str(legacy_audio) + ".timing.json").write_text(
                json.dumps([{"text": "Saved sentence.", "start": 0.0, "end": 1.0}]),
                encoding="utf-8",
            )
            found = reader.NovelReader._find_cached_audio(app, 0, "af_sarah", "1.0")
            self.assertEqual(found, legacy_audio)

    def test_reader_reuses_pre_identity_cache_after_renderer_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio_folder = root / "audio"
            audio_folder.mkdir()
            app = SimpleNamespace(
                novel_path=Path("book.txt"), chapters=[("Chapter 1", "Saved sentence.")],
                audio_folder=audio_folder, output_root=root,
                _renderer_cache_identity=lambda: "new-renderer-identity",
                _book_folder_name=lambda: "book",
                _audio_search_folders=lambda: [audio_folder],
                _normalized_speed=lambda: "1.0",
            )
            app._legacy_cache_file = lambda idx, voice, speed: reader.NovelReader._legacy_cache_file(
                app, idx, voice, speed
            )
            app._cache_names = lambda idx, voice, speed: reader.NovelReader._cache_names(
                app, idx, voice, speed
            )
            app._cached_audio_is_valid = reader.NovelReader._cached_audio_is_valid
            app._create_estimated_timings = lambda *_args: None
            prior_name = reader.NovelReader._cache_names(app, 0, "af_sarah", "1.0")[1]
            prior_audio = audio_folder / prior_name
            sf.write(prior_audio, np.ones(24000, dtype=np.float32), 24000)
            Path(str(prior_audio) + ".timing.json").write_text(
                json.dumps([{"text": "Saved sentence.", "start": 0.0, "end": 1.0}]),
                encoding="utf-8",
            )
            found = reader.NovelReader._find_cached_audio(app, 0, "af_sarah", "1.0")
            self.assertEqual(found, prior_audio)

    def test_reader_discovers_complete_chapter_audio_with_unknown_old_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio_folder = root / "audio"
            audio_folder.mkdir()
            app = SimpleNamespace(
                novel_path=Path("book.txt"), chapters=[("Chapter 1", "Saved sentence.")],
                audio_folder=audio_folder, output_root=root,
                _renderer_cache_identity=lambda: "new-renderer-identity",
                _book_folder_name=lambda: "book",
                _audio_search_folders=lambda: [audio_folder],
                _normalized_speed=lambda: "1.0",
            )
            app._legacy_cache_file = lambda idx, voice, speed: reader.NovelReader._legacy_cache_file(
                app, idx, voice, speed
            )
            app._cache_names = lambda idx, voice, speed: reader.NovelReader._cache_names(
                app, idx, voice, speed
            )
            app._cached_audio_is_valid = reader.NovelReader._cached_audio_is_valid
            app._create_estimated_timings = lambda *_args: None
            old_audio = audio_folder / "chapter_0001_old-renderer-hash.wav"
            sf.write(old_audio, np.ones(24000, dtype=np.float32), 24000)
            Path(str(old_audio) + ".timing.json").write_text(
                json.dumps([{"text": "Saved sentence.", "start": 0.0, "end": 1.0}]),
                encoding="utf-8",
            )
            found = reader.NovelReader._find_cached_audio(app, 0, "af_sarah", "1.0")
            self.assertEqual(found, old_audio)

    def test_cache_accepts_moved_audio_when_content_hashes_still_match(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            sf.write(output, np.ones(24000, dtype=np.float32), 24000)
            timing = Path(str(output) + ".timing.json")
            timing.write_text(json.dumps([{"text": "Saved sentence.", "start": 0.0, "end": 1.0}]), encoding="utf-8")
            completion = {
                "format": "kokoro-chapter-commit-v1",
                "complete": True,
                "renderer_identity": "old-renderer",
                "audio_size": output.stat().st_size,
                "audio_mtime_ns": output.stat().st_mtime_ns - 1,
                "audio_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "timing_sha256": hashlib.sha256(timing.read_bytes()).hexdigest(),
            }
            Path(str(output) + ".complete.json").write_text(json.dumps(completion), encoding="utf-8")
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(output))

    def test_legacy_discovery_accepts_valid_audio_with_stale_completion_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            sf.write(output, np.ones(24000, dtype=np.float32), 24000)
            timing = Path(str(output) + ".timing.json")
            timing.write_text(json.dumps([{"text": "Saved sentence.", "start": 0.0, "end": 1.0}]), encoding="utf-8")
            Path(str(output) + ".complete.json").write_text(json.dumps({
                "format": "kokoro-chapter-commit-v1",
                "complete": True,
                "renderer_identity": "old",
                "audio_size": 1,
                "audio_sha256": "0" * 64,
                "timing_sha256": "0" * 64,
            }), encoding="utf-8")
            self.assertFalse(reader.NovelReader._cached_audio_is_valid(output))
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(
                output, allow_stale_completion=True
            ))

    def test_legacy_discovery_uses_readable_wav_when_timing_sidecar_is_old(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            sf.write(output, np.ones(24000, dtype=np.float32), 24000)
            timing = Path(str(output) + ".timing.json")
            timing.write_text("not valid timing json", encoding="utf-8")
            self.assertTrue(reader.NovelReader._legacy_audio_is_usable(output))

    def test_worker_removes_only_aged_matching_staging_files(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            stale = Path(folder) / "chapter.old.tmp"
            fresh = Path(folder) / "chapter.new.tmp"
            unrelated = Path(folder) / "other.old.tmp"
            for path in (stale, fresh, unrelated):
                path.write_text("temp", encoding="utf-8")
            old = time.time() - 8 * 24 * 3600
            os.utime(stale, (old, old))
            os.utime(unrelated, (old, old))
            self.assertEqual(worker.clean_stale_render_temps(output), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_worker_failure_preserves_existing_cache_files(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            timing = Path(str(output) + ".timing.json")
            output.write_bytes(b"old-wav")
            timing.write_text("old-timing", encoding="utf-8")
            fake = FakeKokoro([(np.ones(1000, dtype=np.float32), 24000), RuntimeError("synthesis failed")])
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(worker.ChapterRenderError) as raised:
                worker.render_one(fake, ["Good.", "Broken."], output, "af_nicole", 1.0, 1, "job")
            self.assertEqual(raised.exception.failed_paragraphs, [2])
            self.assertEqual(output.read_bytes(), b"old-wav")
            self.assertEqual(timing.read_text(encoding="utf-8"), "old-timing")

    def test_interrupted_audio_timing_pair_is_rejected_by_pending_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            timing = Path(str(output) + ".timing.json")
            sf.write(output, np.ones(12000, dtype=np.float32), 24000)
            timing.write_text(json.dumps([{"text": "old", "start": 0, "end": 0.4}]), encoding="utf-8")
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(output))
            fake = FakeKokoro([(np.ones(2400, dtype=np.float32), 24000)])
            real_replace = os.replace
            replace_calls = 0

            def fail_while_replacing_timing(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("simulated interruption between WAV and timing replacement")
                return real_replace(source, destination)

            with mock.patch.object(worker.os, "replace", side_effect=fail_while_replacing_timing):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
                    worker.render_one(fake, ["new"], output, "af_nicole", 1.0, 1, "job")
            self.assertTrue(Path(str(output) + ".pending").exists())
            self.assertFalse(reader.NovelReader._cached_audio_is_valid(output))

    def test_worker_enforces_chapter_duration_limit_before_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            output.write_bytes(b"old audio")
            with mock.patch.object(worker, "MAX_RENDER_DURATION_SECONDS", 0.01):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "rendering limit"):
                    worker.render_one(FakeKokoro([(np.ones(2400, dtype=np.float32), 24000)]),
                                      ["long"], output, "af_nicole", 1.0, 1, "job")
            self.assertEqual(output.read_bytes(), b"old audio")
            self.assertEqual([p.name for p in Path(folder).iterdir()], [output.name])

    def test_worker_rejects_oversized_paragraph_before_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "paragraphs.json"
            source.write_text(json.dumps(["x" * (worker.MAX_PARAGRAPH_CHARACTERS + 1)]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Split the paragraph"):
                worker.load_paragraphs(source)

    def test_worker_resamples_rate_changes_to_first_rate_with_antialias_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            first = np.zeros(2400, dtype=np.float32)
            t = np.arange(4800, dtype=np.float32) / 48000
            second = np.sin(2 * np.pi * 15000 * t).astype(np.float32)
            fake = FakeKokoro([(first, 24000), (second, 48000)])
            with contextlib.redirect_stdout(io.StringIO()):
                worker.render_one(fake, ["First.", "Second."], output, "af_nicole", 1.0, 1, "job")
            info = sf.info(output)
            timings = json.loads(Path(str(output) + ".timing.json").read_text(encoding="utf-8"))
            audio, rate = sf.read(output, dtype="float32")
            self.assertEqual(rate, 24000)
            self.assertEqual(info.frames, 2 * 2400 + 2 * int(24000 * 0.28))
            self.assertAlmostEqual(timings[0]["end"], 0.1, places=3)
            self.assertAlmostEqual(timings[1]["start"], 0.38, places=3)
            resampled_high_frequency = audio[int(0.38 * rate):int(0.48 * rate)]
            self.assertLess(float(np.sqrt(np.mean(resampled_high_frequency ** 2))), 0.02)
            self.assertTrue(Path(str(output) + ".complete.json").is_file())

    def test_worker_rejects_invalid_rates_and_nonfinite_or_empty_samples_before_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            cases = [
                FakeKokoro([(np.array([np.nan], dtype=np.float32), 24000)]),
                FakeKokoro([(np.array([], dtype=np.float32), 24000)]),
                FakeKokoro([(np.ones(100, dtype=np.float32), 1000)]),
            ]
            for fake in cases:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    worker.render_one(fake, ["One.", "Two."], output, "af_nicole", 1.0, 1, "job")
                self.assertFalse(output.exists())
                self.assertFalse(Path(str(output) + ".timing.json").exists())

    def test_reader_cache_validator_accepts_good_bundle_and_rejects_bad_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "chapter.wav"
            sf.write(output, np.ones(24000, dtype=np.float32), 24000)
            timing = Path(str(output) + ".timing.json")
            timing.write_text(json.dumps([{"text": "spoken", "start": 0.0, "end": 0.5}]), encoding="utf-8")
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(output))
            timing.write_text(json.dumps([{"text": "spoken", "start": 0.0, "end": 0.0}]), encoding="utf-8")
            self.assertFalse(reader.NovelReader._cached_audio_is_valid(output))

    def test_preload_completion_always_clears_running_state(self):
        app = SimpleNamespace(
            _closing=False,
            preload_restart_requested=False,
            preloading=True,
            preload_stop_btn=DummyWidget(),
            prefetching={1},
            preload_process=object(),
            preload_cancel_event=object(),
            prefetch_info=DummyWidget(),
            prefetch_bar=DummyWidget(),
            chapters=[("Chapter", "body")],
            _update_voice_status=lambda: None,
            _refresh_chapter_list=lambda: None,
            _refresh_audio_cache_counts=lambda: None,
        )
        reader.NovelReader._preload_all_done(app, [])
        self.assertFalse(app.preloading)
        self.assertIsNone(app.preload_process)
        self.assertIsNone(app.preload_cancel_event)
        self.assertFalse(app.prefetching)
        self.assertEqual(app.preload_state, "complete")

    def test_preload_cancel_then_restart_schedules_exactly_one_new_run(self):
        scheduled = []
        app = SimpleNamespace(
            _closing=False, preload_restart_requested=True, preloading=True,
            preload_state="running", preload_stop_btn=DummyWidget(), prefetching={0},
            preload_process=object(), preload_cancel_event=threading.Event(),
            prefetch_info=DummyWidget(), prefetch_bar=DummyWidget(),
            chapters=[("Chapter", "text")], voice_label=SimpleNamespace(get=lambda: "Heart"),
            _schedule_after=lambda delay, callback: scheduled.append((delay, callback)),
            _start_novel_preload=lambda: None,
            _update_voice_status=lambda: None, _refresh_chapter_list=lambda: None,
            _refresh_audio_cache_counts=lambda: None,
        )
        reader.NovelReader._preload_all_done(app, [], cancelled=True)
        self.assertEqual(app.preload_state, "cancelled")
        self.assertFalse(app.preloading)
        self.assertFalse(app.preload_restart_requested)
        self.assertIsNone(app.preload_process)
        self.assertIsNone(app.preload_cancel_event)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 100)

    def test_rapid_preload_cancel_restart_cycles_remain_consistent(self):
        for _ in range(50):
            scheduled = []
            app = SimpleNamespace(
                _closing=False, preload_restart_requested=True, preloading=True,
                preload_state="running", preload_stop_btn=DummyWidget(), prefetching={0},
                preload_process=object(), preload_cancel_event=threading.Event(),
                prefetch_info=DummyWidget(), prefetch_bar=DummyWidget(),
                chapters=[("Chapter", "text")], voice_label=SimpleNamespace(get=lambda: "Heart"),
                _schedule_after=lambda delay, callback: scheduled.append((delay, callback)),
                _start_novel_preload=lambda: None,
                _update_voice_status=lambda: None, _refresh_chapter_list=lambda: None,
                _refresh_audio_cache_counts=lambda: None,
            )
            reader.NovelReader._preload_all_done(app, [], cancelled=True)
            self.assertEqual(app.preload_state, "cancelled")
            self.assertFalse(app.preloading)
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0][0], 100)

    def test_stop_preload_signals_event_and_terminates_child(self):
        class Process:
            def __init__(self):
                self.terminated = False
            def poll(self):
                return None
            def terminate(self):
                self.terminated = True

        process = Process()
        cancel = threading.Event()
        app = SimpleNamespace(
            preloading=True, preload_cancel_event=cancel, preload_restart_requested=True,
            preload_state="running", preload_process=process,
            preload_stop_btn=DummyWidget(), prefetch_info=DummyWidget(),
        )
        reader.NovelReader.stop_preload(app)
        self.assertTrue(cancel.is_set())
        self.assertTrue(process.terminated)
        self.assertEqual(app.preload_state, "cancelled")
        self.assertFalse(app.preload_restart_requested)
        self.assertEqual(app.preload_stop_btn.options["state"], "disabled")

    def test_shutdown_completion_clears_lifecycle_without_touching_widgets(self):
        class ForbiddenWidget:
            def configure(self, **_kwargs):
                raise AssertionError("must not touch Tk widgets after shutdown")

        app = SimpleNamespace(
            _closing=True, preload_restart_requested=True, preloading=True,
            preload_state="running", preload_stop_btn=ForbiddenWidget(), prefetching={0},
            preload_process=object(), preload_cancel_event=threading.Event(),
        )
        reader.NovelReader._preload_all_done(app, [], cancelled=False)
        self.assertFalse(app.preloading)
        self.assertEqual(app.preload_state, "cancelled")
        self.assertFalse(app.preload_restart_requested)
        self.assertFalse(app.prefetching)
        self.assertIsNone(app.preload_process)
        self.assertIsNone(app.preload_cancel_event)

    def test_bounded_process_termination_escalates_to_kill(self):
        class StubbornProcess:
            def __init__(self):
                self.killed = False
                self.waits = 0
            def poll(self):
                return 0 if self.killed else None
            def terminate(self):
                pass
            def wait(self, timeout):
                self.waits += 1
                if not self.killed:
                    raise subprocess.TimeoutExpired("worker", timeout)
            def kill(self):
                self.killed = True

        process = StubbornProcess()
        self.assertTrue(reader._terminate_process_bounded(process, timeout=0.01))
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 2)

    def test_close_cancels_preload_terminates_children_and_joins_workers(self):
        class Process:
            def __init__(self):
                self.exited = False
                self.terminated = False
            def poll(self):
                return 0 if self.exited else None
            def terminate(self):
                self.terminated = True
                self.exited = True
            def wait(self, timeout):
                return 0
            def kill(self):
                self.exited = True

        cancel = threading.Event()
        worker_finished = threading.Event()
        generation_process, preload_process = Process(), Process()
        worker = threading.Thread(target=lambda: (cancel.wait(2), worker_finished.set()), daemon=True)
        worker.start()
        app = SimpleNamespace(
            _closing=False, _audio_load_generation=0, cache_scan_generation=2,
            playing=False, started_at=None, _save_progress=lambda: None,
            stop_audio=lambda: None, root=SimpleNamespace(destroy=lambda: None),
            preloading=True, preload_state="running", preload_cancel_event=cancel,
            preload_restart_requested=True, generation_process=generation_process,
            preload_process=preload_process, _background_threads={worker},
            _background_threads_lock=threading.Lock(),
        )
        reader.NovelReader.close(app)
        self.assertTrue(app._closing)
        self.assertEqual(app.preload_state, "cancelled")
        self.assertFalse(app.preloading)
        self.assertTrue(generation_process.terminated)
        self.assertTrue(preload_process.terminated)
        self.assertTrue(worker_finished.is_set())
        self.assertFalse(worker.is_alive())

    def test_preload_completion_key_fast_path_is_gone(self):
        source = Path(reader.__file__).read_text(encoding="utf-8")
        self.assertNotIn("preload_completed_key", source)
        self.assertNotIn("preload_run_key", source)

    def test_preload_thread_start_failure_restores_idle_state(self):
        class FailedThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("simulated thread start failure")

        with tempfile.TemporaryDirectory() as folder:
            model_dir = Path(folder)
            (model_dir / "kokoro-v1.0.onnx").write_bytes(b"model")
            (model_dir / "voices-v1.0.bin").write_bytes(b"voices")
            app = SimpleNamespace(
                preloading=False,
                chapters=[("Chapter", "Text.")],
                model_dir=model_dir,
                novel_path=model_dir / "book.txt",
                chapter_idx=0,
                busy=False,
                playing=False,
                voice_label=SimpleNamespace(get=lambda: "Nicole (US female, soft)"),
                speed=SimpleNamespace(get=lambda: "1.0"),
                prefetch_voice_id=None,
                prefetch_speed=None,
                prefetch_ready=set(),
                preload_restart_requested=False,
                preload_cancel_event=None,
                preload_stop_btn=DummyWidget(),
                prefetch_info=DummyWidget(),
                prefetch_bar=DummyWidget(),
                _update_voice_status=lambda: None,
                _refresh_chapter_list=lambda: None,
                _closing=False,
                _background_threads=set(),
                _background_threads_lock=threading.Lock(),
            )
            app._start_daemon = lambda target: reader.NovelReader._start_daemon(app, target)
            with mock.patch.object(reader.threading, "Thread", FailedThread):
                reader.NovelReader._start_novel_preload(app)
            self.assertFalse(app.preloading)
            self.assertIsNone(app.preload_cancel_event)
            self.assertEqual(app.preload_stop_btn.options["state"], "disabled")
            self.assertIn("simulated thread start failure", app.prefetch_info.options["text"])
            self.assertEqual(app.preload_state, "failed")

    def test_cache_identity_changes_when_model_asset_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_dir = root / "model"
            model_dir.mkdir()
            model_file = model_dir / "kokoro-v1.0.onnx"
            voices_file = model_dir / "voices-v1.0.bin"
            model_file.write_bytes(b"model-A")
            voices_file.write_bytes(b"voice-A")
            app = reader.NovelReader.__new__(reader.NovelReader)
            app.model_dir = model_dir
            app.novel_path = root / "book.txt"
            app.chapters = [("One", "A chapter.")]
            app.audio_folder = root / "audio"
            first = app._cache_names(0, "af_nicole", "1.0")
            before = model_file.stat()
            model_file.write_bytes(b"model-B")
            os.utime(model_file, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            changed_metadata = app._cache_names(0, "af_nicole", "1.0")
            self.assertNotEqual(first, changed_metadata)
            same_metadata = model_file.stat()
            model_file.write_bytes(b"model-C")
            os.utime(model_file, ns=(same_metadata.st_atime_ns, same_metadata.st_mtime_ns))
            second_app = reader.NovelReader.__new__(reader.NovelReader)
            second_app.model_dir = model_dir
            second_app.novel_path = root / "book.txt"
            second_app.chapters = [("One", "A chapter.")]
            second_app.audio_folder = root / "audio"
            with mock.patch.object(reader, "_sha256_path", wraps=reader._sha256_path) as hash_file:
                second = second_app._cache_names(0, "af_nicole", "1.0")
                second_app._renderer_cache_identity()
            self.assertNotEqual(changed_metadata, second)
            self.assertEqual(hash_file.call_count, 3)  # Model, voices, and worker; second identity call is memoized.
            self.assertEqual(second_app._cache_file(0, "af_nicole", "1.0").name, second[0])

    def test_aligner_atomic_text_writer_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "result.json"
            destination.write_text("old", encoding="utf-8")
            aligner.atomic_write_text(destination, '{"complete": true}')
            self.assertEqual(destination.read_text(encoding="utf-8"), '{"complete": true}')
            self.assertEqual([item.name for item in Path(folder).iterdir()], [destination.name])

    def test_aligner_rejects_invalid_vosk_timing_and_confidence(self):
        valid = aligner.read_word_timing('{"result":[{"word":"hello","start":0.1,"end":0.4,"conf":0.9}]}')
        self.assertEqual(valid[0]["word"], "hello")
        for invalid in (
            '{"result":[{"word":"x","start":NaN,"end":0.4,"conf":0.9}]}',
            '{"result":[{"word":"x","start":0.4,"end":0.1,"conf":0.9}]}',
            '{"result":[{"word":"x","start":0.1,"end":0.4,"conf":1.1}]}',
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                aligner.read_word_timing(invalid)
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            aligner.read_word_timing(
                '{"result":[{"word":"first","start":0.5,"end":0.7,"conf":0.9},'
                '{"word":"second","start":0.1,"end":0.3,"conf":0.9}]}'
            )
        with self.assertRaisesRegex(ValueError, "moved backward"):
            aligner.align_chapter(
                [{"text": "first second"}],
                [
                    {"word": "first", "norm": "first", "start": 0.5, "end": 0.7, "confidence": 0.9},
                    {"word": "second", "norm": "second", "start": 0.1, "end": 0.3, "confidence": 0.9},
                ],
            )

    def test_alignment_metadata_records_python_vosk_and_model_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "vosk-model-test"
            model.mkdir()
            metadata = aligner.alignment_runtime_metadata(model)
            self.assertEqual(metadata["python_version"], sys.version.split()[0])
            self.assertTrue(metadata["vosk_version"])
            self.assertEqual(metadata["model_name"], "vosk-model-test")
            self.assertEqual(metadata["model_path"], str(model.resolve()))
            initial_fingerprint = metadata["model_metadata_sha256"]
            asset = model / "am" / "final.mdl"
            asset.parent.mkdir()
            asset.write_bytes(b"asset-one")
            first_stat = asset.stat()
            changed = aligner.alignment_runtime_metadata(model)["model_metadata_sha256"]
            asset.write_bytes(b"asset-two-longer")
            os.utime(asset, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns + 1_000_000_000))
            self.assertNotEqual(initial_fingerprint, changed)
            self.assertNotEqual(changed, aligner.alignment_runtime_metadata(model)["model_metadata_sha256"])

    def test_missing_audioop_reports_fallback_before_loading_vosk(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "resample.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b"\0\0" * 2400)
            with mock.patch.object(aligner, "audioop", None), self.assertRaisesRegex(RuntimeError, "audioop-lts"):
                aligner.recognize(audio, Path(folder) / "model-not-loaded")

    def test_reader_only_returns_exact_high_confidence_word_spans(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "chapter.wav"
            sf.write(audio, np.zeros(24000, dtype=np.float32), 24000)
            timing = Path(str(audio) + ".timing.json")
            timing.write_text("[]", encoding="utf-8")
            stat = audio.stat()
            words = {
                "format": "kokoro-vosk-word-timings-v1",
                "accepted_for_highlighting": True,
                "audio_size": stat.st_size,
                "audio_mtime_ns": stat.st_mtime_ns,
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "timing_sha256": hashlib.sha256(timing.read_bytes()).hexdigest(),
                "paragraphs": [{"paragraph": 0, "words": [
                    {"text": "Hello", "start": 0.1, "end": 0.3, "confidence": 0.9, "match": "exact"},
                    {"text": "world", "start": 0.4, "end": 0.6, "confidence": 0.4, "match": "exact"},
                ]}],
            }
            Path(str(audio) + ".words.json").write_text(json.dumps(words), encoding="utf-8")
            app = SimpleNamespace(lines=["Hello world"], timing_line_map=[0], text=DummyText())
            result = reader.NovelReader._load_word_timings(app, audio, timing)
            self.assertEqual(len(result[0]), 1)
            self.assertEqual(result[0][0]["start"], 0.1)
            words["paragraphs"][0]["words"][1]["start"] = 0.05
            words["paragraphs"][0]["words"][1]["end"] = 0.08
            Path(str(audio) + ".words.json").write_text(json.dumps(words), encoding="utf-8")
            self.assertEqual(reader.NovelReader._load_word_timings(app, audio, timing), [[]])
            words["paragraphs"][0]["words"][1]["start"] = 0.4
            words["paragraphs"][0]["words"][1]["end"] = 0.6
            Path(str(audio) + ".words.json").write_text(json.dumps(words), encoding="utf-8")
            original_mtime = audio.stat().st_mtime_ns
            with audio.open("r+b") as stream:
                stream.seek(44)
                stream.write(b"\x01\x00")
            os.utime(audio, ns=(audio.stat().st_atime_ns, original_mtime))
            self.assertEqual(reader.NovelReader._load_word_timings(app, audio, timing), [[]])
            with audio.open("r+b") as stream:
                stream.seek(44)
                stream.write(b"\x00\x00")
            os.utime(audio, ns=(audio.stat().st_atime_ns, original_mtime))
            words["paragraphs"] = []
            Path(str(audio) + ".words.json").write_text(json.dumps(words), encoding="utf-8")
            self.assertEqual(reader.NovelReader._load_word_timings(app, audio, timing), [[]])

    def test_reader_withholds_words_from_low_coverage_paragraph(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "chapter.wav"
            sf.write(audio, np.zeros(24000, dtype=np.float32), 24000)
            timing = Path(str(audio) + ".timing.json")
            timing.write_text("[]", encoding="utf-8")
            stat = audio.stat()
            words = {
                "format": "kokoro-vosk-word-timings-v1", "accepted_for_highlighting": True,
                "audio_size": stat.st_size, "audio_mtime_ns": stat.st_mtime_ns,
                "timing_sha256": hashlib.sha256(timing.read_bytes()).hexdigest(),
                "paragraphs": [{"paragraph": 0, "accepted_for_highlighting": False, "words": [
                    {"text": "Hello", "start": 0.1, "end": 0.3, "confidence": 0.9, "match": "exact"},
                    {"text": "world", "start": 0.4, "end": 0.6, "confidence": 0.9, "match": "exact"},
                ]}],
            }
            Path(str(audio) + ".words.json").write_text(json.dumps(words), encoding="utf-8")
            app = SimpleNamespace(lines=["Hello world"], timing_line_map=[0], text=DummyText())
            self.assertEqual(reader.NovelReader._load_word_timings(app, audio, timing), [[]])

    def test_alignment_records_and_gates_per_paragraph_exact_coverage(self):
        recognized = [
            {"norm": "one", "word": "one", "start": 0.1, "end": 0.2, "confidence": 0.9},
            {"norm": "two", "word": "two", "start": 0.2, "end": 0.3, "confidence": 0.9},
        ]
        output, _ = aligner.align_chapter(
            [{"text": "one two three"}, {"text": "missing words"}], recognized
        )
        self.assertEqual(output[0]["exact_coverage"], 2 / 3)
        self.assertEqual(output[0]["substitution_count"], 0)
        self.assertAlmostEqual(output[0]["mean_confidence"], 0.9)
        self.assertTrue(output[0]["accepted_for_highlighting"])
        self.assertEqual(output[1]["exact_coverage"], 0.0)
        self.assertFalse(output[1]["accepted_for_highlighting"])

    def test_alignment_budget_rejects_oversized_input_before_matching(self):
        text = "word " * (aligner.MAX_ALIGNMENT_TOKENS + 1)
        with self.assertRaisesRegex(RuntimeError, "too large for safe whole-chapter alignment"):
            aligner.align_chapter([{"text": text}], [])

    def test_bounded_alignment_worker_returns_valid_alignment(self):
        paragraphs, recognized = aligner.align_chapter_bounded(
            [{"text": "one two"}],
            [
                {"word": "one", "norm": "one", "start": 0.1, "end": 0.2, "confidence": 0.9},
                {"word": "two", "norm": "two", "start": 0.2, "end": 0.3, "confidence": 0.9},
            ], timeout=60,
        )
        self.assertEqual(paragraphs[0]["exact_coverage"], 1.0)
        self.assertEqual(recognized["exact"], 2)

    def test_bounded_alignment_worker_terminates_at_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            worker_script = Path(folder) / "slow_alignment_worker.py"
            worker_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            with mock.patch.object(aligner, "__file__", str(worker_script)):
                with self.assertRaisesRegex(RuntimeError, "processing budget"):
                    aligner.align_chapter_bounded([], [], timeout=1)

    def test_settings_sanitizer_recovers_invalid_progress_and_speed(self):
        settings, recovered = reader._sanitize_settings({
            "schema_version": 1,
            "novels": {"book": {"chapter_idx": -1, "offset": float("nan"), "speed": float("inf"), "voice": "invalid"}},
        })
        record = settings["novels"]["book"]
        self.assertTrue(recovered)
        self.assertEqual(record["chapter_idx"], 0)
        self.assertEqual(record["offset"], 0.0)
        self.assertEqual(record["speed"], 1.0)
        self.assertIsNone(reader._bounded_float("nan", reader.MIN_SPEED, reader.MAX_SPEED))

    def test_settings_sanitizer_drops_malformed_paths_and_deduplicates_history(self):
        settings, recovered = reader._sanitize_settings({
            "schema_version": 1,
            "model_dir": "bad\0path",
            "output_root": "  ",
            "novels": {"book": {"audio_folder": "bad\0folder", "audio_folder_history": [
                "C:/Audio", "c:/audio", "", 17, "bad\0folder",
            ]}},
        })
        record = settings["novels"]["book"]
        self.assertTrue(recovered)
        self.assertNotIn("model_dir", settings)
        self.assertNotIn("output_root", settings)
        self.assertEqual(record["audio_folder"], "")
        self.assertEqual(len(record["audio_folder_history"]), 1)

    def test_corrupt_settings_backup_never_overwrites_previous_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            settings.write_text("{bad json", encoding="utf-8")
            first = reader.NovelReader._backup_corrupt_settings(settings)
            settings.write_text("still bad", encoding="utf-8")
            second = reader.NovelReader._backup_corrupt_settings(settings)
            self.assertEqual(first.read_text(encoding="utf-8"), "{bad json")
            self.assertEqual(second.read_text(encoding="utf-8"), "still bad")
            self.assertNotEqual(first, second)

    def test_settings_atomic_save_preserves_old_file_on_replace_error(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "settings.json"
            target.write_text("old-settings", encoding="utf-8")
            app = SimpleNamespace(
                settings_path=target,
                settings={"novels": {}},
                status=DummyWidget(),
            )
            with mock.patch.object(reader.os, "replace", side_effect=OSError("simulated disk error")):
                reader.NovelReader._save_settings(app)
            self.assertEqual(target.read_text(encoding="utf-8"), "old-settings")
            self.assertIn("Could not save settings", app.status.options["text"])
            self.assertEqual([item.name for item in Path(folder).iterdir()], [target.name])

    def test_worker_cli_reports_versioned_structured_validation_error(self):
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            model = folder_path / "model.onnx"
            voices = folder_path / "voices.bin"
            input_file = folder_path / "paragraphs.json"
            model.write_bytes(b"dummy")
            voices.write_bytes(b"dummy")
            input_file.write_text("[]", encoding="utf-8")
            command = [
                sys.executable, str(Path(worker.__file__)), "single",
                "--model", str(model), "--voices", str(voices), "--voice", "af_nicole",
                "--speed", "1.0", "--input", str(input_file), "--output", str(folder_path / "out.wav"),
            ]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            event = json.loads(result.stdout.splitlines()[0])
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(event["protocol"], 1)
            self.assertTrue(event["job_id"])
            self.assertEqual(event["event"], "worker_error")

    def test_single_worker_accepts_actual_novel_chapter_number(self):
        args = worker.build_parser().parse_args([
            "single", "--model", "model.onnx", "--voices", "voices.bin",
            "--voice", "af_nicole", "--speed", "1.0", "--chapter", "78",
            "--input", "paragraphs.json", "--output", "chapter.wav",
        ])
        self.assertEqual(args.chapter, 78)

    @unittest.skipUnless(os.environ.get("KOKORO_RUN_SLOW_INTEGRATION") == "1", "Slow model integration is opt-in; the local inference run exceeded 240 seconds")
    def test_real_kokoro_vosk_pipeline_writes_consistent_sidecars(self):
        model = ROOT / "kokoro-v1.0.onnx"
        voices = ROOT / "voices-v1.0.bin"
        vosk_model = ROOT / "vosk-model-small-en-us-0.15"
        if not model.is_file() or not voices.is_file() or not vosk_model.is_dir():
            self.skipTest("Local Kokoro/Vosk model assets are not available")
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            input_file = folder_path / "paragraphs.json"
            output = folder_path / "chapter.wav"
            input_file.write_text(json.dumps(["Hello there, this is a short alignment check."]), encoding="utf-8")
            render = subprocess.run(
                [
                    sys.executable, str(Path(worker.__file__)), "single",
                    "--model", str(model), "--voices", str(voices), "--voice", "af_nicole",
                    "--speed", "1.0", "--input", str(input_file), "--output", str(output),
                ], text=True, capture_output=True, check=False, timeout=240,
            )
            self.assertEqual(render.returncode, 0, render.stderr + "\n" + render.stdout)
            events = [json.loads(line) for line in render.stdout.splitlines() if line.startswith("{")]
            self.assertTrue(events)
            self.assertTrue(all(event.get("protocol") == 1 for event in events))
            self.assertEqual(events[-1]["event"], "chapter_done")
            self.assertGreater(sf.info(output).frames, 0)
            timings_path = Path(str(output) + ".timing.json")
            self.assertTrue(reader.NovelReader._cached_audio_is_valid(output))

            aligned = subprocess.run(
                [sys.executable, str(Path(aligner.__file__)), str(output), str(vosk_model)],
                text=True, capture_output=True, check=False, timeout=240,
            )
            self.assertEqual(aligned.returncode, 0, aligned.stderr + "\n" + aligned.stdout)
            words_path = Path(str(output) + ".words.json")
            words = json.loads(words_path.read_text(encoding="utf-8"))
            self.assertEqual(words["format"], "kokoro-vosk-word-timings-v1")
            self.assertEqual(words["timing_sha256"], hashlib.sha256(timings_path.read_bytes()).hexdigest())
            self.assertTrue(Path(str(output) + ".recognized.txt").is_file())

    def test_vosk_aligner_processes_24khz_wav_and_writes_sidecars(self):
        vosk_model = ROOT / "vosk-model-small-en-us-0.15"
        if not vosk_model.is_dir():
            self.skipTest("Local Vosk model is not available")
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "silence.wav"
            sf.write(audio, np.zeros(24000, dtype=np.float32), 24000, subtype="PCM_16")
            Path(str(audio) + ".timing.json").write_text(
                json.dumps([{"text": "hello", "start": 0.0, "end": 0.7}]), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(Path(aligner.__file__)), str(audio), str(vosk_model)],
                text=True, capture_output=True, check=False, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
            words = json.loads(Path(str(audio) + ".words.json").read_text(encoding="utf-8"))
            self.assertEqual(words["format"], "kokoro-vosk-word-timings-v1")
            self.assertEqual(words["audio_sha256"], hashlib.sha256(audio.read_bytes()).hexdigest())
            self.assertIsInstance(words["paragraphs"], list)

    def test_reader_import_survives_missing_optional_modules(self):
        code = """
import importlib.util, sys
for name in ('sounddevice', 'soundfile', 'pymupdf'):
    sys.modules[name] = None
spec = importlib.util.spec_from_file_location('reader_without_optional_audio', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.sd is None and module.sf is None and module.fitz is None
assert len(module.OPTIONAL_IMPORT_ERRORS) == 3
"""
        result = subprocess.run([sys.executable, "-c", code, str(Path(reader.__file__))], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
