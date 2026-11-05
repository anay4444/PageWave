# PageWave

PageWave is a Windows desktop reader that turns chapter-based text into speech with Kokoro ONNX. It supports chapter audio caching, background preparation, playback position saving, and optional Vosk word timing alignment.

## Requirements

- Windows 10 or newer
- Python 3.12 or newer
- Kokoro ONNX model files: `kokoro-v1.0.onnx` and `voices-v1.0.bin` (kept outside this repository)

## Setup

From PowerShell in the project folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Place the Kokoro model and voice files in a local folder, then start PageWave and select that folder in the app:

```powershell
python .\kokoro_novel_reader.py
```

Vosk alignment is optional. The Vosk model is not included; choose or install its model separately when using word-timing alignment.

## Tests

Run the deterministic test suite with:

```powershell
python -m unittest discover -s .\tests -v
```

The real Kokoro inference integration test is opt-in because model generation can take several minutes on CPU.

## Project files

- `kokoro_novel_reader.py` — Windows desktop reader and playback controls
- `kokoro_paragraph_worker.py` — Kokoro chapter rendering worker
- `vosk_align_chapter.py` — optional word alignment utility
- `Kokoro_Worker_Protocol.md` — worker event protocol
- `tests/` — automated regression tests

Model weights, Vosk model data, generated audio, and machine-specific analysis files are intentionally excluded from version control.
