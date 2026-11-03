"""The web app's side of the parser sandbox. With SANDBOX_DIR set (as on the server), a file goes
to the no-network parser container and the app waits for its answer. Without it (local runs and
tests) the same parser code runs in-process."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from . import parser_sandbox
from .config import settings
from .statement import StatementError, parse


class SandboxError(Exception):
    """The sandbox refused a file. `message` is safe to show the visitor."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _root() -> Path | None:
    d = settings().sandbox_dir
    return Path(d) if d else None


def _exchange(suffix: str, data: bytes, timeout: float = 25.0) -> tuple[dict, Path]:
    root = _root()
    assert root is not None
    job = uuid.uuid4().hex
    in_dir, out_dir = root / "in", root / "out"
    in_dir.mkdir(exist_ok=True)
    out_dir.mkdir(exist_ok=True)
    tmp = in_dir / f".{job}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, in_dir / f"{job}{suffix}")
    result = out_dir / f"{job}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result.exists():
            try:
                obj = json.loads(result.read_text())
            finally:
                result.unlink(missing_ok=True)
            return obj, out_dir / f"{job}.wav"
        time.sleep(0.1)
    (in_dir / f"{job}{suffix}").unlink(missing_ok=True)
    raise SandboxError("The file took too long to read. Please try again.")


def read_statement(data: bytes) -> dict:
    """Parse a statement CSV in the sandbox and return Statement.as_dict()."""
    max_bytes = settings().limits.csv_max_bytes
    if len(data) > max_bytes:
        raise SandboxError(f"The statement is larger than {max_bytes // 1024} KB.")
    if _root() is None:
        try:
            return parse(data, max_bytes=max_bytes).as_dict()
        except StatementError as exc:
            raise SandboxError(str(exc)) from None
    obj, _ = _exchange(".csv", data)
    if not obj.get("ok"):
        raise SandboxError(str(obj.get("error") or "The statement could not be read."))
    return obj["statement"]


def prepare_voice(data: bytes) -> tuple[bytes, float]:
    """Check a voice note's length in the sandbox and get it back as 16 kHz mono WAV."""
    lim = settings().limits
    if len(data) > lim.voice_max_bytes:
        raise SandboxError(f"Voice notes can be up to {lim.voice_max_seconds} seconds.")
    if _root() is None:
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "note.audio", Path(d)
            src.write_bytes(data)
            parser_sandbox.handle_audio(src, out, lim.voice_max_seconds)
            obj = json.loads((out / "note.json").read_text())
            wav_path = out / "note.wav"
            if not obj.get("ok"):
                raise SandboxError(obj.get("error") or "That recording could not be read.")
            return wav_path.read_bytes(), float(obj.get("seconds") or 0)
    obj, wav_path = _exchange(".audio", data)
    try:
        if not obj.get("ok"):
            raise SandboxError(str(obj.get("error") or "That recording could not be read."))
        return wav_path.read_bytes(), float(obj.get("seconds") or 0)
    finally:
        wav_path.unlink(missing_ok=True)
