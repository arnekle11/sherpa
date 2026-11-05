"""Admin commands from the web UI, executed by the service.

The web process never talks to Shellies or Telegram itself. It drops a small JSON file into
<data>/commands; the service takes it (read + delete, so it runs at most once), re-validates it
and runs it through its normal code paths. Unknown kinds, bad parameters and stale files
(e.g. left over from before a restart) are discarded, never executed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger("sherpa.commands")

KINDS = ("telegram_test", "test_event")
MAX_AGE_S = 60            # older command files are ignored
TEST_EVENT_MAX_S = 60     # test events are short on purpose
TEST_EVENT_COOLDOWN_S = 30
_SOURCE = re.compile(r"^[0-9A-Fa-f:.]{1,45}$")  # an IP address, for the audit message


def command_dir(db_path: str | Path) -> Path:
    return Path(db_path).parent / "commands"


def validate(cmd: dict) -> dict | None:
    """Return a clean command, or None if it must not run."""
    if not isinstance(cmd, dict) or cmd.get("kind") not in KINDS:
        return None
    ts = cmd.get("ts")
    if not isinstance(ts, (int, float)) or not (0 <= time.time() - ts <= MAX_AGE_S):
        return None
    source = cmd.get("source", "")
    out = {"kind": cmd["kind"], "ts": ts, "source": source if isinstance(source, str) and _SOURCE.match(source) else "?"}
    if cmd["kind"] == "test_event":
        d = cmd.get("duration_s")
        if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= TEST_EVENT_MAX_S:
            return None
        out["duration_s"] = d
    return out


def submit(directory: str | Path, kind: str, source: str, **params) -> None:
    """Web side: write atomically (the service only reads *.json, never the .tmp)."""
    cmd = validate({"kind": kind, "ts": time.time(), "source": source, **params})
    if cmd is None:
        raise ValueError(f"invalid command {kind!r}")
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{secrets.token_hex(4)}"
    tmp = d / f"{name}.tmp"
    tmp.write_text(json.dumps(cmd), encoding="utf-8")
    os.replace(tmp, d / f"{name}.json")


def take(directory: str | Path) -> list[dict]:
    """Service side: claim every pending command (deleted before it runs, so never twice)."""
    d = Path(directory)
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            raw = f.read_text(encoding="utf-8")
            f.unlink()
        except OSError:
            continue  # vanished or unreadable: another taker or a permissions problem; never run twice
        try:
            cmd = validate(json.loads(raw))
        except ValueError:
            cmd = None
        if cmd is None:
            log.warning("discarded invalid or stale command file %s", f.name)
            continue
        out.append(cmd)
    for f in d.glob("*.tmp"):  # leftovers from a crashed writer
        try:
            if time.time() - f.stat().st_mtime > MAX_AGE_S:
                f.unlink()
        except OSError:
            pass
    return out
