"""Durable limits, the copy token, and the parser sandbox."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, limits, parser_sandbox, seed, workspace
from app.sandbox import SandboxError, prepare_voice


@pytest.mark.db
def test_a_limit_counts_up_to_its_ceiling_and_then_refuses(db_url):
    with db.tx() as c:
        c.execute("DELETE FROM rate_counters WHERE bucket LIKE 't:%%'")
    for _ in range(3):
        with db.tx() as c:
            limits.check(c, [("t:one", 3, limits.HOUR)], "no")
    with pytest.raises(limits.LimitReached) as e:
        with db.tx() as c:
            limits.check(c, [("t:one", 3, limits.HOUR)], "Enough for now.")
    assert e.value.message == "Enough for now."


@pytest.mark.db
def test_several_limits_count_together_or_not_at_all(db_url):
    with db.tx() as c:
        c.execute("DELETE FROM rate_counters WHERE bucket LIKE 't:%%'")
        limits.check(c, [("t:b", 1, limits.DAY)], "no")
    with pytest.raises(limits.LimitReached):
        with db.tx() as c:
            limits.check(c, [("t:a", 5, limits.DAY), ("t:b", 1, limits.DAY)], "no")
    with db.tx() as c:
        a = c.execute("SELECT coalesce(sum(n), 0) AS n FROM rate_counters WHERE bucket = 't:a'").fetchone()["n"]
    assert a == 0


@pytest.mark.db
def test_a_copys_own_limits_never_roll_over_and_go_with_the_copy(workspace):
    bucket = f"msg:ws:{workspace}"
    with db.tx() as c:
        limits.check(c, [(bucket, 2, limits.COPY)], "no")
        limits.check(c, [(bucket, 2, limits.COPY)], "no")
        row = c.execute("SELECT window_start FROM rate_counters WHERE bucket = %s", (bucket,)).fetchone()
    assert row["window_start"] == limits.LIFETIME
    with pytest.raises(limits.LimitReached):
        with db.tx() as c:
            limits.check(c, [(bucket, 2, limits.COPY)], "no")
    with db.tx() as c:
        c.execute("DELETE FROM workspaces WHERE id = %s", (workspace,))
        limits.prune(c)
        assert c.execute("SELECT 1 FROM rate_counters WHERE bucket = %s", (bucket,)).fetchone() is None


def test_addresses_are_stored_only_as_keyed_hashes(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k1")
    from app import config

    config.reset_settings()
    h = limits.addr_hash("198.51.100.7")
    assert "198" not in h and len(h) == 32
    monkeypatch.setenv("SECRET_KEY", "k2")
    config.reset_settings()
    assert limits.addr_hash("198.51.100.7") != h
    config.reset_settings()


def test_copy_tokens_are_signed_and_expire(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "token-test")
    from app import config

    config.reset_settings()
    wid = "0a5d58cf-bff8-4a32-979c-c604ae0bec54"
    t = workspace.make_token(wid, datetime.now(timezone.utc) + timedelta(hours=1))
    assert workspace.read_token(t) == wid
    a, b, sig = t.split(".")
    assert workspace.read_token(f"{a}.{int(b) + 9999}.{sig}") is None
    assert workspace.read_token(workspace.make_token(wid, datetime.now(timezone.utc) - timedelta(seconds=1))) is None
    assert workspace.read_token("x.y") is None
    config.reset_settings()


def test_the_sandbox_reads_a_statement_and_deletes_the_upload(tmp_path):
    src = tmp_path / "in" / "job1.csv"
    src.parent.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    src.write_bytes(seed.statement_csv())
    parser_sandbox.handle_csv(src, out, 20 * 1024)
    assert not src.exists()
    obj = json.loads((out / "job1.json").read_text())
    assert obj["ok"] and len(obj["statement"]["lines"]) == len(seed.STATEMENT_ROWS)


def test_the_sandbox_names_the_row_not_the_content(tmp_path):
    src = tmp_path / "bad.csv"
    src.write_text("Date,Description,Amount,Balance\n2026-10-01,PRIVATE-WORDS,abc,1.00\n")
    parser_sandbox.handle_csv(src, tmp_path, 20 * 1024)
    obj = json.loads((tmp_path / "bad.json").read_text())
    assert not obj["ok"] and "PRIVATE-WORDS" not in obj["error"] and "row" in obj["error"].lower()


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="no ffmpeg")


def _tone(path: Path, seconds: float) -> bytes:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "libvorbis", str(path)], check=True)
    return path.read_bytes()


@needs_ffmpeg
def test_a_voice_note_comes_back_as_16khz_mono_wav(tmp_path):
    wav, seconds = prepare_voice(_tone(tmp_path / "a.ogg", 2))
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert int.from_bytes(wav[24:28], "little") == 16000 and int.from_bytes(wav[22:24], "little") == 1
    assert 1.5 < seconds < 2.6


@needs_ffmpeg
def test_a_voice_note_over_thirty_seconds_is_refused(tmp_path):
    with pytest.raises(SandboxError) as e:
        prepare_voice(_tone(tmp_path / "long.ogg", 35))
    assert "30 seconds" in e.value.message


def test_a_file_that_is_not_audio_is_refused(tmp_path):
    with pytest.raises(SandboxError):
        prepare_voice(b"#!/bin/sh\necho hi\n" * 20)
