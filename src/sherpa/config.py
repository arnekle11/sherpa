"""YAML config, validated strictly (unknown keys are errors, not silently ignored)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedCfg(_Base):
    url: str
    poll_interval_s: float = Field(2.0, ge=0.1)
    timeout_s: float = Field(3.0, gt=0)
    max_age_min: float = Field(10.0, ge=0)
    down_after_s: float = Field(60.0, gt=0)
    expected_link_contains: str | None = "OD/52034"
    user_agent: str = "SHERPA/0.1 (private first-responder notifier)"
    max_response_bytes: int = Field(2_000_000, gt=0)


class DeviceCfg(_Base):
    name: str
    ip: str
    generation: Literal[1, 2]
    model: str = ""
    channel: int = Field(0, ge=0)
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
