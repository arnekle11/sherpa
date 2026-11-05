"""Editing config.yaml from the web UI: whitelist of fields, strict validation, atomic save.

Only the fields below can be changed here. The feed URL, the self-link canary, the DB path and the
Telegram settings are deliberately not editable from the web (Telegram secrets live in .env only).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import AppCfg

MIN_POLL_S = 2.0  # the feed operator's rate limit is unknown: never poll faster from the UI

# (section, key) -> (parser, label, hint)
FEED_FIELDS = {
    "poll_interval_s": (float, "Poll interval", f"seconds between feed polls, minimum {MIN_POLL_S:g}"),
    "max_age_min": (float, "Max age at startup", "on startup, items older than this are ignored"),
    "down_after_s": (float, "Feed down after", "alert if no healthy response for this long"),
}
ALERT_FIELDS = {
    "repeat_min": (float, "Alert repeat", "re-send a still-active alert every N minutes, 0 = once"),
    "heartbeat_min": (float, "Heartbeat", "status message every N minutes, 0 = off"),
    "device_check_interval_s": (float, "Device check", "read-only reachability probe of every Shelly, 0 = off"),
    "deadman_interval_s": (float, "Dead-man ping", "external ping period, min 10; needs SHERPA_DEADMAN_URL in .env"),
}
DEVICE_FIELDS = {
    "name": str, "ip": str, "generation": int, "model": str, "channel": int,
    "on_duration_s": int, "timeout_s": float, "attempts": int, "retry_delay_s": float,
}
DEVICE_DEFAULTS = {"name": "", "ip": "", "generation": 1, "model": "", "channel": 0,
                   "on_duration_s": 300, "timeout_s": 3.0, "attempts": 4, "retry_delay_s": 0.5}
_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
_NAME = re.compile(r"^[A-Za-z0-9 ._\-]{1,40}$")
_DEV_KEY = re.compile(r"^dev\.(\d+)\.(\w+)$")


def config_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def load_raw(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _num(kind, text: str, label: str, errors: list[str]):
    try:
        v = kind(text.strip())
    except ValueError:
        errors.append(f"{label}: '{text}' is not a valid {'whole number' if kind is int else 'number'}")
        return None
    if isinstance(v, float) and not math.isfinite(v):
        errors.append(f"{label}: must be a finite number")
        return None
    return v


def build(raw: dict, form: dict[str, str]) -> tuple[dict, list[str]]:
    """Return (new raw config, human-readable errors). Fields not in the whitelist are kept as they are."""
    errors: list[str] = []
    new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    new.setdefault("feed", {})
    new.setdefault("alerts", {})
    for section, fields in (("feed", FEED_FIELDS), ("alerts", ALERT_FIELDS)):
        for key, (kind, label, _hint) in fields.items():
            if (name := f"{section}.{key}") in form:
                v = _num(kind, form[name], label, errors)
                if v is not None:
                    new[section][key] = v
    poll = new["feed"].get("poll_interval_s")
    if isinstance(poll, (int, float)) and poll < MIN_POLL_S:
        errors.append(f"Poll interval: minimum is {MIN_POLL_S:g} s (the operator's rate limit is unknown)")

    rows: dict[int, dict[str, str]] = {}
    for k, v in form.items():
        if m := _DEV_KEY.match(k):
            rows.setdefault(int(m[1]), {})[m[2]] = v
    devices = []
    for idx in sorted(rows):
        r = rows[idx]
        if r.get("delete"):
            continue
        if r.get("new") and not r.get("name", "").strip():
            continue  # the blank "add a device" row
        dev: dict = {}
        label = f"Device {len(devices) + 1}"
        for key, kind in DEVICE_FIELDS.items():
            text = r.get(key, "")
            if kind is str:
                dev[key] = text.strip()
            elif text.strip() == "":
                errors.append(f"{label} {key}: required")
            else:
                v = _num(kind, text, f"{label} {key}", errors)
                if v is not None:
                    dev[key] = v
        if not _NAME.fullmatch(dev.get("name", "")):
            errors.append(f"{label} name: 1-40 letters, digits, space . _ - only")
        if not _HOST.fullmatch(dev.get("ip", "")):
            errors.append(f"{label} ip: a host or IP, optionally with :port")
        devices.append(dev)
    new["devices"] = devices
    return new, errors


def validate(new: dict) -> list[str]:
    try:
        AppCfg.model_validate(new)
    except ValidationError as e:
        return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
    return []


def save(path: str | Path, form: dict[str, str], expected_hash: str) -> list[str]:
    """Apply a submitted form. Returns errors; an empty list means the file was written."""
    path = Path(path)
    try:
        if config_hash(path) != expected_hash:
            return ["The settings changed since you opened this page. Reload and apply your edit again."]
        new, errors = build(load_raw(path), form)
    except (OSError, yaml.YAMLError) as e:
        return [f"Cannot read {path.name}: {e}"]
    errors += validate(new) if not errors else []
    if errors:
        return errors
    text = yaml.safe_dump(new, sort_keys=False, allow_unicode=True)
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError:
        pass  # a missing backup must not block a valid change
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        shutil.copymode(path, tmp)
        try:
            os.replace(tmp, path)  # atomic: the service never sees a half-written file
        except OSError:  # e.g. the file itself is bind-mounted: write in place
            path.write_text(text, encoding="utf-8")
            tmp.unlink(missing_ok=True)
    except OSError as e:
        return [f"Cannot write {path.name}: {e}"]
    return []
