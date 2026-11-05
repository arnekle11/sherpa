"""Editing config.yaml from the web UI: whitelist of fields, strict validation, atomic save.

Only the fields below can be changed here. The feed URL, the self-link canary, the DB path and the
Telegram settings are deliberately not editable from the web (Telegram secrets live in .env only).
The UI bounds are tighter than the config model's: they keep a hurried edit from creating a dangerous
setup (e.g. a huge startup max-age would make the next restart fire the LED for every item in the feed).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import AppCfg

MIN_POLL_S = 2.0  # the feed operator's rate limit is unknown: never poll faster from the UI

# (section, key) -> (parser, label, hint)
FEED_FIELDS = {
    "poll_interval_s": (float, "Poll interval", f"seconds between feed polls, {MIN_POLL_S:g}-60"),
    "max_age_min": (float, "Max age at startup", "after a (re)start, older items are recorded but do not trigger; 1-120"),
    "down_after_s": (float, "Feed down after", "alert if no healthy response for this long; 10-3600"),
}
ALERT_FIELDS = {
    "repeat_min": (float, "Alert repeat", "re-send a still-active alert every N minutes; 0 = once"),
    "heartbeat_min": (float, "Heartbeat", "status message every N minutes; 0 = off, else at least 10"),
    "device_check_interval_s": (float, "Device check", "read-only reachability check of every Shelly; 0 = off, else 30-3600"),
    "deadman_interval_s": (float, "Dead-man ping", "external ping period, 10-3600; needs SHERPA_DEADMAN_URL in .env"),
}
DEVICE_FIELDS = {
    "name": str, "ip": str, "generation": int, "model": str, "channel": int,
    "on_duration_s": int, "timeout_s": float, "attempts": int, "retry_delay_s": float,
}
DEVICE_DEFAULTS = {"name": "", "ip": "", "generation": 1, "model": "", "channel": 0,
                   "on_duration_s": 300, "timeout_s": 3.0, "attempts": 4, "retry_delay_s": 0.5}
DEVICE_LABELS = {"name": "name", "ip": "address", "generation": "generation", "model": "model label",
                 "channel": "relay channel", "on_duration_s": "on duration", "timeout_s": "timeout",
                 "attempts": "attempts", "retry_delay_s": "retry delay"}

# field -> (min, max, zero_allowed)
BOUNDS = {
    "feed.poll_interval_s": (MIN_POLL_S, 60, False),
    "feed.max_age_min": (1, 120, False),
    "feed.down_after_s": (10, 3600, False),
    "alerts.repeat_min": (1, 1440, True),
    "alerts.heartbeat_min": (10, 10080, True),
    "alerts.device_check_interval_s": (30, 3600, True),
    "alerts.deadman_interval_s": (10, 3600, False),
    "dev.channel": (0, 3, False),
    "dev.on_duration_s": (10, 3600, False),
    "dev.timeout_s": (0.5, 10, False),
    "dev.attempts": (1, 10, False),
    "dev.retry_delay_s": (0, 10, False),
}
_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
_NAME = re.compile(r"^[A-Za-z0-9 ._\-]{1,40}$")
_DEV_KEY = re.compile(r"^dev\.(\d+)\.(\w+)$")
HEADER = ("# Managed by the SHERPA web UI (last saved {when}).\n"
          "# Comments are not kept when saving here; see config.example.yaml for what each setting means.\n")


class FieldError(str):
    """An error message that also knows which form field it belongs to (None = the form as a whole)."""
    field: str | None

    def __new__(cls, msg: str, field: str | None = None):
        obj = super().__new__(cls, msg)
        obj.field = field
        return obj


def config_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def load_raw(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _num(kind, text: str, label: str, field: str, errors: list[FieldError]):
    try:
        v = kind(text.strip())
    except ValueError:
        errors.append(FieldError(f"{label}: '{text}' is not a valid {'whole number' if kind is int else 'number'}", field))
        return None
    if isinstance(v, float) and not math.isfinite(v):
        errors.append(FieldError(f"{label}: must be a finite number", field))
        return None
    return v


def _bounded(bound_key: str, v, label: str, field: str, errors: list[FieldError]) -> bool:
    lo, hi, zero_ok = BOUNDS[bound_key]
    if zero_ok and v == 0:
        return True
    if lo <= v <= hi:
        return True
    rng = f"{lo:g}-{hi:g}" + (" (or 0 to turn it off)" if zero_ok else "")
    if bound_key == "feed.poll_interval_s" and v < MIN_POLL_S:
        msg = f"{label}: minimum is {MIN_POLL_S:g} s (the feed operator's rate limit is unknown)"
    elif bound_key == "feed.max_age_min" and v > hi:
        msg = (f"{label}: at most {hi:g} min. Items stay in the feed for about 31 h; a large value would make the "
               "next restart switch the LED on for old emergencies.")
    else:
        msg = f"{label}: must be {rng}"
    errors.append(FieldError(msg, field))
    return False


def _build(raw: dict, form: dict[str, str]) -> tuple[dict, list[FieldError], list[int]]:
    """(new raw config, errors, form row index of every resulting device)."""
    errors: list[FieldError] = []
    new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    new.setdefault("feed", {})
    new.setdefault("alerts", {})
    for section, fields in (("feed", FEED_FIELDS), ("alerts", ALERT_FIELDS)):
        for key, (kind, label, _hint) in fields.items():
            name = f"{section}.{key}"
            if name in form:
                v = _num(kind, form[name], label, name, errors)
                if v is not None and _bounded(name, v, label, name, errors):
                    new[section][key] = v

    rows: dict[int, dict[str, str]] = {}
    for k, v in form.items():
        if m := _DEV_KEY.match(k):
            rows.setdefault(int(m[1]), {})[m[2]] = v
    devices, rowmap = [], []
    for idx in sorted(rows):
        r = rows[idx]
        if r.get("delete"):
            continue
        if r.get("new") and not r.get("name", "").strip():
            continue  # the blank "add a device" row
        dev: dict = {}
        label = f"Device {len(devices) + 1}"
        for key, kind in DEVICE_FIELDS.items():
            field = f"dev.{idx}.{key}"
            flabel = f"{label} {DEVICE_LABELS[key]}"
            text = r.get(key, "")
            if kind is str:
                dev[key] = text.strip()
            elif text.strip() == "":
                errors.append(FieldError(f"{flabel}: required", field))
            else:
                v = _num(kind, text, flabel, field, errors)
                if v is not None and (f"dev.{key}" not in BOUNDS or _bounded(f"dev.{key}", v, flabel, field, errors)):
                    dev[key] = v
        if not _NAME.fullmatch(dev.get("name", "")):
            errors.append(FieldError(f"{label} name: 1-40 letters, digits, space . _ - only", f"dev.{idx}.name"))
        if not _HOST.fullmatch(dev.get("ip", "")):
            errors.append(FieldError(f"{label} address: an IP or host name, optionally with :port", f"dev.{idx}.ip"))
        devices.append(dev)
        rowmap.append(idx)
    addresses = [(d.get("ip", "").lower(), d.get("channel", 0)) for d in devices]
    for i, a in enumerate(addresses):
        if a[0] and addresses.index(a) != i:
            errors.append(FieldError(f"Device {i + 1} address: the same relay is already used by device "
                                     f"{addresses.index(a) + 1}", f"dev.{rowmap[i]}.ip"))
    new["devices"] = devices
    return new, errors, rowmap


def build(raw: dict, form: dict[str, str]) -> tuple[dict, list[FieldError]]:
    """Return (new raw config, human-readable errors). Fields not in the whitelist are kept as they are."""
    new, errors, _ = _build(raw, form)
    return new, errors


def _field_for(loc: tuple, rowmap: list[int]) -> str | None:
    if len(loc) >= 2 and loc[0] in ("feed", "alerts"):
        return f"{loc[0]}.{loc[1]}"
    if len(loc) >= 3 and loc[0] == "devices" and isinstance(loc[1], int) and loc[1] < len(rowmap):
        return f"dev.{rowmap[loc[1]]}.{loc[2]}"
    return None


def validate(new: dict, rowmap: list[int] | None = None) -> list[FieldError]:
    try:
        AppCfg.model_validate(new)
    except ValidationError as e:
        out = []
        for err in e.errors():
            loc = tuple(err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            if loc == ("devices",) and "at least 1" in msg:
                msg = "at least one device is required (the last one cannot be removed)"
            where = ".".join(str(p) for p in loc)
            out.append(FieldError(f"{where}: {msg}" if where else msg, _field_for(loc, rowmap or [])))
        return out
    return []


def save(path: str | Path, form: dict[str, str], expected_hash: str) -> list[FieldError]:
    """Apply a submitted form. Returns errors; an empty list means the file was written."""
    path = Path(path)
    try:
        if config_hash(path) != expected_hash:
            return [FieldError("The settings changed since you opened this page. Reload and apply your edit again.")]
        new, errors, rowmap = _build(load_raw(path), form)
    except (OSError, yaml.YAMLError) as e:
        return [FieldError(f"Cannot read {path.name}: {e}")]
    errors += validate(new, rowmap) if not errors else []
    if errors:
        return errors
    text = HEADER.format(when=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")) + \
        yaml.safe_dump(new, sort_keys=False, allow_unicode=True)
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
        return [FieldError(f"Cannot write {path.name}: {e}")]
    return []
