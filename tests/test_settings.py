import pytest
import yaml

from sherpa import settings
from sherpa.config import load_config

BASE = """\
feed: {url: 'http://x/feed', poll_interval_s: 2, expected_link_contains: OD/52034}
devices:
- {name: led-1, ip: 192.168.1.50, generation: 1}
alerts: {repeat_min: 30}
db_path: /data/sherpa.db
"""


def _form(path, **over):
    cfg = load_config(path)
    f = {"feed.poll_interval_s": str(cfg.feed.poll_interval_s), "feed.max_age_min": str(cfg.feed.max_age_min),
         "feed.down_after_s": str(cfg.feed.down_after_s), "alerts.repeat_min": str(cfg.alerts.repeat_min),
         "alerts.heartbeat_min": "0", "alerts.device_check_interval_s": "300"}
    for i, d in enumerate(cfg.devices):
        for k in settings.DEVICE_FIELDS:
            f[f"dev.{i}.{k}"] = str(getattr(d, k))
    n = len(cfg.devices)
    f.update({f"dev.{n}.new": "1", f"dev.{n}.name": ""})
    f.update(over)
    return f


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(BASE)
    return p


def test_edit_saves_and_keeps_non_editable_keys(cfgfile):
    h = settings.config_hash(cfgfile)
    assert settings.save(cfgfile, _form(cfgfile, **{"feed.max_age_min": "15", "alerts.heartbeat_min": "360"}), h) == []
    cfg = load_config(cfgfile)
    assert cfg.feed.max_age_min == 15 and cfg.alerts.heartbeat_min == 360
    assert cfg.feed.url == "http://x/feed" and cfg.db_path == "/data/sherpa.db"
    assert (cfgfile.parent / "config.yaml.bak").read_text() == BASE


def test_poll_interval_below_minimum_rejected(cfgfile):
    h = settings.config_hash(cfgfile)
    errs = settings.save(cfgfile, _form(cfgfile, **{"feed.poll_interval_s": "1"}), h)
    assert errs and "minimum" in errs[0]
    assert cfgfile.read_text() == BASE


@pytest.mark.parametrize("field,val", [("feed.down_after_s", "abc"), ("feed.max_age_min", "-1"),
                                       ("alerts.repeat_min", "nan"), ("feed.down_after_s", "inf")])
def test_bad_numbers_rejected(cfgfile, field, val):
    errs = settings.save(cfgfile, _form(cfgfile, **{field: val}), settings.config_hash(cfgfile))
    assert errs and cfgfile.read_text() == BASE


def test_add_and_delete_devices(cfgfile):
    h = settings.config_hash(cfgfile)
    f = _form(cfgfile, **{"dev.1.name": "led-2", "dev.1.ip": "10.0.0.5:8080", "dev.1.generation": "2",
                          "dev.1.model": "", "dev.1.channel": "0", "dev.1.on_duration_s": "300",
                          "dev.1.timeout_s": "3", "dev.1.attempts": "4", "dev.1.retry_delay_s": "0.5"})
    assert settings.save(cfgfile, f, h) == []
    assert [d.name for d in load_config(cfgfile).devices] == ["led-1", "led-2"]
    f = _form(cfgfile, **{"dev.0.delete": "on"})
    assert settings.save(cfgfile, f, settings.config_hash(cfgfile)) == []
    assert [d.name for d in load_config(cfgfile).devices] == ["led-2"]


def test_cannot_delete_last_device_or_use_bad_host(cfgfile):
    h = settings.config_hash(cfgfile)
    assert settings.save(cfgfile, _form(cfgfile, **{"dev.0.delete": "on"}), h)
    assert settings.save(cfgfile, _form(cfgfile, **{"dev.0.ip": "http://evil/x"}), h)
    assert cfgfile.read_text() == BASE


def test_duplicate_device_names_rejected(cfgfile):
    h = settings.config_hash(cfgfile)
    f = _form(cfgfile, **{"dev.1.name": "led-1", "dev.1.ip": "10.0.0.5", "dev.1.generation": "1",
                          "dev.1.channel": "0", "dev.1.on_duration_s": "300", "dev.1.timeout_s": "3",
                          "dev.1.attempts": "4", "dev.1.retry_delay_s": "0.5"})
    assert any("unique" in e for e in settings.save(cfgfile, f, h))


def test_stale_form_rejected(cfgfile):
    h = settings.config_hash(cfgfile)
    cfgfile.write_text(BASE + "log_level: DEBUG\n")
    errs = settings.save(cfgfile, _form(cfgfile), h)
    assert errs and "changed since" in errs[0]
