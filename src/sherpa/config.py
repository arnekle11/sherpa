"""YAML config, validated strictly (unknown keys are errors, not silently ignored)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedCfg(_Base):
    url: str
    poll_interval_s: float = Field(2.0, ge=0.1)
    timeout_s: float = Field(3.0, gt=0)
    max_age_min: float = Field(10.0, ge=0)
    down_after_s: float = Field(60.0, gt=0)
    expected_link_contains: str | None = "OD/52034"
    user_agent: str = "SHERPA (private first-responder notifier)"
    max_response_bytes: int = Field(2_000_000, gt=0)


class DeviceCfg(_Base):
    name: str
    ip: str
    generation: Literal[1, 2]
    model: str = ""
    channel: int = Field(0, ge=0)
    # Gen2 component that drives the output: switch (relays), or light/rgb/rgbw (dimmer and LED-strip profiles)
    component: Literal["switch", "light", "rgb", "rgbw"] = "switch"
    # What a real alert looks like on colour/dimmer outputs. Empty/0 = leave the device's own colour/brightness.
    alert_color: str = Field("", pattern=r"^(#[0-9a-fA-F]{6})?$")
    alert_brightness: int = Field(0, ge=0, le=100)
    on_duration_s: int = Field(300, ge=1)
    timeout_s: float = Field(3.0, gt=0)
    attempts: int = Field(4, ge=1)
    retry_delay_s: float = Field(0.5, ge=0)


class AlertsCfg(_Base):
    heartbeat_min: float = Field(0, ge=0)
    repeat_min: float = Field(30, ge=0)
    device_check_interval_s: float = Field(300, ge=0)
    deadman_interval_s: float = Field(60, ge=10)  # ping period when SHERPA_DEADMAN_URL is set
    telegram_api_base: str = "https://api.telegram.org"


class AppCfg(_Base):
    feed: FeedCfg
    devices: list[DeviceCfg] = Field(min_length=1)
    alerts: AlertsCfg = AlertsCfg()
    db_path: str = "data/sherpa.db"
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        if v.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return v.upper()

    @model_validator(mode="after")
    def _unique_names(self) -> "AppCfg":
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("device names must be unique")
        return self


def load_config(path: str | Path) -> AppCfg:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AppCfg.model_validate(raw)


def load_config_with_fallback(path: str | Path, fallback: str | Path | None) -> tuple[AppCfg, str | None]:
    """(config, error). If the file is invalid but a last-good copy exists, use that and return the error,
    so a bad hand edit or an update that renamed a setting never stops SHERPA from starting."""
    try:
        return load_config(path), None
    except Exception as e:
        if fallback and Path(fallback).is_file():
            try:
                return load_config(fallback), str(e)
            except Exception:
                pass
        raise


def save_last_good(path: str | Path, fallback: str | Path | None) -> None:
    """Keep a copy of a config that just loaded fine (atomic; only rewritten when it changed)."""
    if not fallback:
        return
    data = Path(path).read_bytes()
    fb = Path(fallback)
    if fb.is_file() and fb.read_bytes() == data:
        return
    fb.parent.mkdir(parents=True, exist_ok=True)
    tmp = fb.with_name(fb.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, fb)


def telegram_from_env() -> tuple[str, str] | None:
    token = os.environ.get("SHERPA_TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("SHERPA_TELEGRAM_CHAT_ID", "").strip()
    return (token, chat) if token and chat else None


def deadman_url_from_env() -> str | None:
    url = os.environ.get("SHERPA_DEADMAN_URL", "").strip()
    return url if url.startswith(("http://", "https://")) else None


def describe_changes(old: AppCfg, new: AppCfg, limit: int = 12) -> list[str]:
    """Human-readable differences, for the 'settings changed' notice. Devices are matched by name."""
    out: list[str] = []
    o, n = old.model_dump(), new.model_dump()
    for section in ("feed", "alerts"):
        for k in n[section]:
            if o[section].get(k) != n[section][k]:
                out.append(f"{section}.{k}: {o[section].get(k)} -> {n[section][k]}")
    for k in ("db_path", "log_level"):
        if o[k] != n[k]:
            out.append(f"{k}: {o[k]} -> {n[k]}")
    od = {d["name"]: d for d in o["devices"]}
    nd = {d["name"]: d for d in n["devices"]}
    for name in nd.keys() - od.keys():
        out.append(f"device {name} added ({nd[name]['ip']})")
    for name in od.keys() - nd.keys():
        out.append(f"device {name} removed")
    for name in nd.keys() & od.keys():
        for k, v in nd[name].items():
            if od[name][k] != v:
                out.append(f"device {name}: {k} {od[name][k]} -> {v}")
    if len(out) > limit:
        out = out[:limit] + [f"... and {len(out) - limit} more"]
    return out
