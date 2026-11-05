"""Small LAN web UI: dashboard, event + alert history, and (after login) settings.

Runs as its own process (`sherpa web`) so a bug here can never stop the poll loop. It reads the
SQLite DB and status.json written by the service, and edits config.yaml; the service restarts
itself to apply a valid change. Stdlib only, no JavaScript, strict CSP; all dynamic text is escaped.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import __version__, settings
from .config import load_config
from .store import LAG_SQL, lag_summary

log = logging.getLogger("sherpa.web")

SESSION_IDLE_S = 30 * 60
MAX_BODY = 64 * 1024
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 5, 300
STALE_STATUS_S = 30

CSS = """
:root{--bg:#f6f7f9;--fg:#1b1f24;--card:#fff;--line:#d8dce1;--mute:#5b6570;--ok:#17803d;--bad:#c4271d;--warn:#a15c00;--acc:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e9ed;--card:#1d2127;--line:#343b44;--mute:#9aa4af;--ok:#4cc27a;--bad:#ff6b60;--warn:#e0a030;--acc:#6aa5ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line)}
header b{font-size:17px}nav a{margin-right:14px;color:var(--acc);text-decoration:none}nav a:hover{text-decoration:underline}
header .sp{flex:1}main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:16px}
h1,h2{margin:0 0 10px;font-size:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.v{font-size:20px;font-weight:600}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mute{color:var(--mute)}
.banner{padding:10px 14px;border-radius:8px;margin-bottom:16px;font-weight:600;border:1px solid}
.banner.ok{border-color:var(--ok)}.banner.bad{border-color:var(--bad)}.banner.warn{border-color:var(--warn)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-size:12px;text-transform:uppercase}.scroll{overflow-x:auto}
label{display:block;font-size:13px;color:var(--mute);margin:8px 0 2px}input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);width:100%}
input[type=checkbox]{width:auto}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 14px}
.hint{font-size:12px;color:var(--mute)}button{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.link{background:none;border:none;color:var(--acc);padding:0}.err{border:1px solid var(--bad);border-radius:8px;padding:10px 14px;margin-bottom:16px}
.err ul{margin:4px 0 0 18px;padding:0}.dev{border-top:1px solid var(--line);padding-top:8px;margin-top:10px}
code{background:var(--bg);padding:1px 5px;border-radius:4px}
"""

_e = html.escape


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _e(iso)


def fmt_dur(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h else "") + (f"{m}m " if m and not d else "") + (f"{s}s" if not (d or h) else "")


class Web:
    """Shared state for all requests."""

    def __init__(self, config_path: str, password: str | None):
        self.config_path = Path(config_path)
        self.password = password or None
        self.sessions: dict[str, dict] = {}
        self.fails: dict[str, list[float]] = {}
        cfg = load_config(config_path)
        self.db_path = Path(cfg.db_path)
        self.status_path = self.db_path.parent / "status.json"

    # ---- data ----
    def status(self) -> dict | None:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            return list(db.execute(sql, args))
        except sqlite3.Error as e:
            log.warning("db query failed: %s", e)
            return []
        finally:
            db.close()

    # ---- auth ----
    def new_session(self) -> tuple[str, dict]:
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if now - v["seen"] < SESSION_IDLE_S}
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = {"csrf": secrets.token_urlsafe(24), "seen": now}
        return tok, self.sessions[tok]

    def session(self, token: str | None) -> dict | None:
        s = self.sessions.get(token or "")
        if s and time.monotonic() - s["seen"] < SESSION_IDLE_S:
            s["seen"] = time.monotonic()
            return s
        self.sessions.pop(token or "", None)
        return None

    def locked_out(self, ip: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
        self.fails[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILS

    def check_password(self, ip: str, given: str) -> bool:
        if self.password is None or self.locked_out(ip):
            return False
        if hmac.compare_digest(given.encode(), self.password.encode()):
            self.fails.pop(ip, None)
            return True
        self.fails.setdefault(ip, []).append(time.monotonic())
        time.sleep(1)  # slow down guessing
        return False


# ---------------------------------------------------------------- pages
def page(title: str, body: str, user: bool, active: str, refresh: int = 0) -> str:
    nav = "".join(f'<a href="{href}"{" style=font-weight:700" if key == active else ""}>{name}</a>'
                  for key, href, name in (("d", "/", "Dashboard"), ("e", "/events", "Events"),
                                          ("a", "/alerts", "Alerts"), ("s", "/settings", "Settings")))
    who = ('<form method="post" action="/logout" style="margin:0"><button class="link" type="submit">Log out</button></form>'
           if user else '<a href="/login" style="color:var(--acc)">Log in</a>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{meta}<title>{_e(title)} - SHERPA</title><link rel="stylesheet" href="/static.css"></head><body>'
            f'<header><b>SHERPA</b><nav>{nav}</nav><span class="sp"></span>{who}</header><main>{body}</main></body></html>')


def dashboard(w: Web, user: bool) -> str:
    st = w.status()
    if st is None:
        banner = '<div class="banner bad">No status from the service yet (not started, or data volume not shared).</div>'
    else:
        age = time.time() - st["written"]
        if age > STALE_STATUS_S:
            banner = f'<div class="banner bad">Service looks DOWN: last status {fmt_dur(age)} ago.</div>'
        elif st["active_alerts"]:
            banner = ('<div class="banner warn">Active alerts: '
                      + ", ".join(f"<code>{_e(a)}</code>" for a in st["active_alerts"]) + "</div>")
        else:
            banner = '<div class="banner ok">Service running, no active alerts.</div>'
    out = [banner]
    if st:
        def tile(k, v, cls=""):
            return f'<div><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'
        feed_age = st["feed_ok_age_s"]
        out.append('<div class="card"><h2>Service</h2><div class="grid">'
                   + tile("Uptime", fmt_dur(st["uptime_s"]))
                   + tile("Poll interval", f'{st["poll_interval_s"]:g} s')
                   + tile("Polls ok / failed", f'{st["polls_ok"]} / {st["polls_failed"]}', "bad" if st["polls_failed"] else "")
                   + tile("Polls skipped", st["polls_skipped"])
                   + tile("Last good feed response", f"{feed_age:.0f} s ago", "bad" if feed_age > 60 else "")
                   + tile("Events since start", st["events_triggered"])
                   + tile("Last event", fmt_ts(st["last_event_at"]))
                   + tile("Version", _e(str(st["version"]))) + "</div></div>")
        rows = "".join(
            f'<tr><td>{_e(n)}</td><td>{_e(d["ip"])}</td>'
            f'<td class="{"ok" if d["ok"] else ("mute" if d["ok"] is None else "bad")}">{_e(d["detail"])}</td>'
            f'<td>{fmt_ts(d["checked"])}</td></tr>' for n, d in st["devices"].items())
        out.append('<div class="card"><h2>Shelly devices</h2><div class="scroll"><table><tr><th>Name</th><th>Address</th>'
                   f'<th>State</th><th>Checked</th></tr>{rows}</table></div></div>')
    lag = lag_summary([r["lag_s"] for r in w.query(LAG_SQL)])
    if lag:
        out.append(f'<div class="card"><h2>Feed lag (first seen - pubDate)</h2><p>{lag["n"]} events: min {lag["min"]:.1f} s, '
                   f'median {lag["median"]:.1f} s, p95 {lag["p95"]:.1f} s, max {lag["max"]:.1f} s</p></div>')
    out.append('<div class="card"><h2>Latest events</h2>' + events_table(w, 10) + "</div>")
    return page("Dashboard", "".join(out), user, "d", refresh=10)


STATE_CLS = {"triggered": "ok", "partial": "warn", "failed": "bad", "pending": "warn", "abandoned": "bad"}


def events_table(w: Web, n: int) -> str:
    rows = w.query("SELECT * FROM events ORDER BY first_seen DESC LIMIT ?", (n,))
    if not rows:
        return '<p class="mute">No events recorded yet. An empty feed is normal.</p>'
    body = "".join(
        f'<tr><td>{fmt_ts(r["first_seen"])}</td><td>{_e(r["title"] or "")}{" <span class=mute>(test)</span>" if r["is_test"] else ""}</td>'
        f'<td>{"%.1f s" % r["lag_s"] if r["lag_s"] is not None else "-"}</td>'
        f'<td class="{STATE_CLS.get(r["state"], "mute")}">{_e(r["state"])}</td><td>{_e(r["detail"] or "")}</td></tr>' for r in rows)
    return ('<div class="scroll"><table><tr><th>First seen</th><th>Event</th><th>Lag</th><th>State</th><th>Detail</th></tr>'
            f"{body}</table></div>")


def events_page(w: Web, user: bool) -> str:
    return page("Events", '<div class="card"><h2>Events (last 200)</h2>' + events_table(w, 200) + "</div>", user, "e")


def alerts_page(w: Web, user: bool) -> str:
    rows = w.query("SELECT * FROM alert_log ORDER BY id DESC LIMIT 200")
    icon = {"raise": ("bad", "RAISED"), "repeat": ("bad", "STILL"), "clear": ("ok", "CLEARED"), "info": ("mute", "info")}
    body = "".join(
        f'<tr><td>{fmt_ts(r["ts"])}</td><td class="{icon.get(r["kind"], ("mute", ""))[0]}">{icon.get(r["kind"], ("", r["kind"]))[1]}</td>'
        f'<td>{_e(r["key"] or "")}</td><td>{_e(r["text"])}</td></tr>' for r in rows)
    table = ('<div class="scroll"><table><tr><th>Time</th><th>Kind</th><th>Alert</th><th>Message</th></tr>' + body + "</table></div>"
             if rows else '<p class="mute">No alerts recorded yet.</p>')
    return page("Alerts", '<div class="card"><h2>Alerts sent (last 200)</h2>' + table + "</div>", user, "a")


def login_page(error: str = "", disabled: bool = False) -> str:
    if disabled:
        body = ('<div class="card"><h2>Editing disabled</h2><p>Set <code>SHERPA_WEB_PASSWORD</code> in <code>.env</code> '
                "and restart the web service to enable login and settings changes.</p></div>")
    else:
        body = ('<div class="card" style="max-width:380px"><h2>Log in</h2>'
                + (f'<p class="bad">{_e(error)}</p>' if error else "")
                + '<form method="post" action="/login"><label for="p">Password</label>'
                  '<input id="p" name="password" type="password" autocomplete="current-password" autofocus>'
                  '<p><button type="submit">Log in</button></p></form></div>')
    return page("Log in", body, False, "")


def settings_page(w: Web, sess: dict | None, errors: list[str] | None = None, saved: bool = False,
                  form: dict[str, str] | None = None) -> str:
    if sess is None:
        return page("Settings", '<div class="card"><h2>Settings</h2><p>Log in to view and change settings.</p>'
                                '<p><a href="/login" style="color:var(--acc)">Log in</a></p></div>', False, "s")
    try:
        raw = load_config(w.config_path).model_dump()  # effective values: keys omitted from the file show defaults
        digest = settings.config_hash(w.config_path)
    except Exception as e:
        return page("Settings", f'<div class="err">Cannot read config: {_e(str(e))}</div>', True, "s")
    f = form if form is not None else {}

    def val(name: str, default) -> str:
        return _e(str(f.get(name, default)))

    out = []
    if saved:
        out.append('<div class="banner ok">Saved. The service restarts itself within a few seconds to apply the change '
                   "(Telegram will say so).</div>")
    if errors:
        out.append('<div class="err"><b>Not saved:</b><ul>' + "".join(f"<li>{_e(x)}</li>" for x in errors) + "</ul></div>")

    def fields(section: str, spec: dict) -> str:
        cur = raw.get(section, {})
        return "".join(
            f'<div><label for="{section}.{k}">{_e(label)}</label><input id="{section}.{k}" name="{section}.{k}" '
            f'inputmode="decimal" value="{val(f"{section}.{k}", cur.get(k, ""))}"><div class="hint">{_e(hint)}</div></div>'
            for k, (_t, label, hint) in spec.items())

    out.append(f'<form method="post" action="/settings"><input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
               f'<input type="hidden" name="base" value="{_e(digest)}">'
               f'<div class="card"><h2>Feed</h2><div class="fields">{fields("feed", settings.FEED_FIELDS)}</div></div>'
               f'<div class="card"><h2>Alerts</h2><div class="fields">{fields("alerts", settings.ALERT_FIELDS)}</div></div>')
    devs = [dict(settings.DEVICE_DEFAULTS, **d) for d in raw.get("devices", [])]
    if form is not None:  # re-render what the user typed
        idxs = sorted({int(k.split(".")[1]) for k in form if k.startswith("dev.") and k.split(".")[1].isdigit()})
        devs = [{k: f.get(f"dev.{i}.{k}", "") for k in settings.DEVICE_FIELDS} | {"_del": f.get(f"dev.{i}.delete"),
                "_new": f.get(f"dev.{i}.new")} for i in idxs]
    else:
        devs.append(dict(settings.DEVICE_DEFAULTS, _new="1"))
    cards = []
    for i, d in enumerate(devs):
        is_new = bool(d.get("_new"))
        inputs = "".join(
            (f'<div><label for="dev.{i}.generation">generation</label><select id="dev.{i}.generation" name="dev.{i}.generation">'
             + "".join(f'<option value="{g}"{" selected" if str(d["generation"]) == str(g) else ""}>Gen{g}</option>' for g in (1, 2))
             + "</select></div>") if k == "generation" else
            f'<div><label for="dev.{i}.{k}">{k}</label><input id="dev.{i}.{k}" name="dev.{i}.{k}" value="{_e(str(d[k]))}"></div>'
            for k in settings.DEVICE_FIELDS)
        extra = (f'<input type="hidden" name="dev.{i}.new" value="1">' if is_new else
                 f'<label><input type="checkbox" name="dev.{i}.delete"{" checked" if d.get("_del") else ""}> remove this device</label>')
        cards.append(f'<div class="dev"><b>{"Add a device (leave the name empty to skip)" if is_new else _e(str(d["name"]))}</b>'
                     f'<div class="fields">{inputs}</div>{extra}</div>')
    out.append('<div class="card"><h2>Shelly devices</h2><div class="hint">on_duration_s is the device-side auto-off timer.</div>'
               + "".join(cards) + "</div>")
    out.append('<p><button type="submit">Save settings</button></p></form>')
    out.append('<p class="hint">Telegram token/chat id, feed URL and the database path are not editable here. '
               "Saving rewrites config.yaml (comments are dropped; the previous file is kept as config.yaml.bak).</p>")
    return page("Settings", "".join(out), True, "s")


# ---------------------------------------------------------------- HTTP
def make_handler(w: Web):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SHERPA-web"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no query strings / cookies in logs
            log.info("%s %s", self.address_string(), fmt % args if args else fmt)

        # -- helpers
        def _cookie(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["sid"].value if "sid" in c else None

        def _session(self) -> dict | None:
            return w.session(self._cookie())

        def _send(self, status: int, body: str | bytes, ctype="text/html; charset=utf-8", headers: dict | None = None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; "
                                                        "frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, to: str, headers: dict | None = None):
            self._send(HTTPStatus.SEE_OTHER, "", headers={"Location": to, **(headers or {})})

        def _form(self) -> dict[str, str] | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large", "text/plain")
                return None
            return dict(parse_qsl(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True))

        def _csrf_ok(self, sess: dict | None, form: dict) -> bool:
            return sess is not None and hmac.compare_digest(form.get("csrf", ""), sess["csrf"])

        # -- routes
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            sess = self._session()
            try:
                if path == "/static.css":
                    self._send(200, CSS, "text/css; charset=utf-8", {"Cache-Control": "max-age=3600"})
                elif path == "/healthz":
                    self._send(200, "ok", "text/plain")
                elif path == "/":
                    self._send(200, dashboard(w, bool(sess)))
                elif path == "/events":
                    self._send(200, events_page(w, bool(sess)))
                elif path == "/alerts":
                    self._send(200, alerts_page(w, bool(sess)))
                elif path == "/settings":
                    self._send(200, settings_page(w, sess))
                elif path == "/login":
                    self._send(200, login_page(disabled=w.password is None))
                else:
                    self._send(404, page("Not found", '<div class="card">Not found.</div>', bool(sess), ""))
            except Exception:
                log.exception("request failed: %s", path)
                self._send(500, "internal error", "text/plain")

        def do_POST(self):
            path = urlsplit(self.path).path
            form = self._form()
            if form is None:
                return
            sess = self._session()
            try:
                if path == "/login":
                    if w.password is None:
                        return self._send(200, login_page(disabled=True))
                    ip = self.client_address[0]
                    if w.locked_out(ip):
                        return self._send(429, login_page("Too many failed attempts. Try again in a few minutes."))
                    if not w.check_password(ip, form.get("password", "")):
                        return self._send(401, login_page("Wrong password."))
                    tok, _ = w.new_session()
                    log.info("login from %s", ip)
                    return self._redirect("/settings", {"Set-Cookie": f"sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_IDLE_S}"})
                if path == "/logout":
                    w.sessions.pop(self._cookie() or "", None)  # harmless to force, so no CSRF token needed
                    return self._redirect("/", {"Set-Cookie": "sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                if path == "/settings":
                    if sess is None:
                        return self._redirect("/login")
                    if not self._csrf_ok(sess, form):
                        return self._send(403, "bad CSRF token", "text/plain")
                    errors = settings.save(w.config_path, form, form.get("base", ""))
                    if errors:
                        return self._send(400, settings_page(w, sess, errors=errors, form=form))
                    log.warning("settings changed via web from %s", self.client_address[0])
                    return self._send(200, settings_page(w, sess, saved=True))
                self._send(404, "not found", "text/plain")
            except Exception:
                log.exception("request failed: %s", path)
                self._send(500, "internal error", "text/plain")

    return Handler


def serve(config_path: str, password: str | None, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    w = Web(config_path, password)
    if w.password is None:
        log.warning("SHERPA_WEB_PASSWORD not set: web UI is read-only, settings cannot be changed")
    srv = ThreadingHTTPServer((host, port), make_handler(w))
    srv.daemon_threads = True
    srv.web = w  # type: ignore[attr-defined]
    log.info("web UI v%s on http://%s:%d", __version__, host, srv.server_address[1])
    return srv
