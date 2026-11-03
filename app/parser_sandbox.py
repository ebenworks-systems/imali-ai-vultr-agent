"""The parser sandbox: the only place an uploaded file is opened.

It runs in its own container with no network at all, a read-only root file system, no Linux
capabilities and small memory and CPU limits (deploy/docker-compose.yml). The web app drops a
file into SANDBOX_DIR/in and waits for the answer in SANDBOX_DIR/out:

- a bank statement CSV comes back as checked JSON (app/statement.py), or as an error naming the
  row and column, never the file's content;
- a voice note is probed with ffprobe, refused when it is longer than the limit or not an audio
  format we accept, and converted to 16 kHz mono WAV, which is all that leaves the sandbox. The
  original is deleted at once and the WAV is deleted by the web app as soon as it is transcribed.

Run it with `python -m app.parser_sandbox`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .statement import StatementError, parse

AUDIO_FORMATS = {"matroska,webm", "webm", "ogg", "mov,mp4,m4a,3gp,3g2,mj2", "wav", "mp3", "aac", "flac"}
POLL_SECONDS = 0.15


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def handle_csv(src: Path, out_dir: Path, max_bytes: int) -> None:
    job = src.stem
    try:
        data = src.read_bytes()
        st = parse(data, max_bytes=max_bytes)
        result = {"ok": True, "statement": st.as_dict()}
    except StatementError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception:  # never let a file crash the sandbox, and never say why outside it
        result = {"ok": False, "error": "The statement could not be read."}
    finally:
        src.unlink(missing_ok=True)
    _write_json(out_dir / f"{job}.json", result)


def probe_audio(path: Path) -> tuple[str, float]:
    """The container format and duration of an audio file, from ffprobe."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(path)],
        capture_output=True, timeout=10, check=False,
    )
    if p.returncode != 0:
        raise ValueError("probe")
    fmt = json.loads(p.stdout or b"{}").get("format") or {}
    name = str(fmt.get("format_name") or "")
    try:
        seconds = float(fmt.get("duration"))
    except (TypeError, ValueError):
        seconds = -1.0
    return name, seconds


def to_wav(src: Path, dst: Path, max_seconds: int) -> None:
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-t", str(max_seconds), "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "wav", str(dst)],
        capture_output=True, timeout=20, check=False,
    )
    if p.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
        raise ValueError("convert")


def handle_audio(src: Path, out_dir: Path, max_seconds: int) -> None:
    job = src.stem
    wav_tmp = out_dir / f".{job}.wav.tmp"
    try:
        name, seconds = probe_audio(src)
        if name not in AUDIO_FORMATS:
            result = {"ok": False, "error": "That recording is not in a format the agent can hear."}
        elif seconds > max_seconds + 0.5:
            result = {"ok": False, "error": f"Voice notes can be up to {max_seconds} seconds."}
        else:
            to_wav(src, wav_tmp, max_seconds)
            os.replace(wav_tmp, out_dir / f"{job}.wav")
            result = {"ok": True, "seconds": round(max(seconds, 0.0), 1)}
    except Exception:
        result = {"ok": False, "error": "That recording could not be read."}
    finally:
        src.unlink(missing_ok=True)
        wav_tmp.unlink(missing_ok=True)
    _write_json(out_dir / f"{job}.json", result)


def run(root: Path, *, once: bool = False) -> None:
    in_dir, out_dir = root / "in", root / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_max = int(os.environ.get("CSV_MAX_BYTES", 20 * 1024))
    voice_max = int(os.environ.get("VOICE_MAX_SECONDS", 30))
    while True:
        for src in sorted(in_dir.iterdir()):
            if src.name.startswith("."):
                continue
            if src.suffix == ".csv":
                handle_csv(src, out_dir, csv_max)
            elif src.suffix == ".audio":
                handle_audio(src, out_dir, voice_max)
            else:
                src.unlink(missing_ok=True)
        # Outputs nobody collected within two minutes are removed.
        now = time.time()
        for f in out_dir.iterdir():
            try:
                if now - f.stat().st_mtime > 120:
                    f.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        if once:
            return
        time.sleep(POLL_SECONDS)


def main() -> None:
    root = Path(os.environ.get("SANDBOX_DIR") or tempfile.gettempdir())
    print(f"parser sandbox watching {root}", file=sys.stderr, flush=True)
    run(root)


if __name__ == "__main__":
    main()
