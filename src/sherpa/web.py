"""Small LAN web UI: dashboard, event + alert history, and (after login) settings.

Runs as its own process (`sherpa web`) so a bug here can never stop the poll loop. It reads the
SQLite DB and status.json written by the service, and edits config.yaml; the service restarts
itself to apply a valid change. Stdlib only, no JavaScript, strict CSP; all dynamic text is escaped.
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
import threading
import logging
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import BUILD, commands, lights, settings, webexport, webpages, webstats
from .config import AppCfg, load_config, load_config_with_fallback
from .devices import capability_of
from .webstyle import CSS, favicon

log = logging.getLogger("sherpa.web")

SESSION_IDLE_S = 30 * 60
COOKIE = "sherpa_sid"
MAX_CONNECTIONS = 32
MAX_BODY = 64 * 1024
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 5, 300

TOOL_DONE = {
    "restored": "The last good settings were restored. The service picks them up within a few seconds.",
    "telegram_test": "Telegram test requested. The message, or a note that Telegram is not configured, appears under Alerts within a few seconds.",
    "light_on": "Switch-on requested. The light turns itself off when the time is up. The result appears below within a few seconds.",
    "light_effect": "Effect requested. It runs until the time is up or you turn it off; the result appears below within a few seconds.",
    "light_off": "Switch-off requested. The result appears below within a few seconds.",
    "test_event": "Test event requested. Every device switches on for the chosen time; the result appears under Alerts and Events.",
}
TOOL_ERR = {
    "no_backup": "There is no last-good copy to restore (the service writes it after each valid start).",
    "restore_failed": "Restoring failed; see the web log.",
    "service_down": "The service is not running, so nothing can execute the request.",
    "confirm": "Tick the confirmation box to run a test event.",
    "duration": f"Duration must be a whole number from 1 to {commands.TEST_EVENT_MAX_S} seconds.",
    "light": "That device or duration is not valid.",
    "light_busy": "A real event was seen recently and its light may still be on. Switching it off now is refused; "
                  "its own timer turns it off.",
    "write": "Could not queue the request; see the web log.",
    "cooldown": f"Another test event ran less than {commands.TEST_EVENT_COOLDOWN_S} s ago. Wait a moment.",
    "real_event": "A real event was seen recently and its LED may still be on. A test now would cut that alert "
                  "short, so it is refused. Try again after the on-duration has passed.",
}


class Web:
    """Shared state for all requests."""

    def __init__(self, config_path: str, password: str | None, fallback: str | None = None):
        self.config_path = Path(config_path)
        self.fallback = Path(fallback) if fallback else None
        self.password = password or None
        self.sessions: dict[str, dict] = {}
        self.fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()  # the server is threaded; these dicts are shared
        self.db_error: str | None = None
        cfg, _ = load_config_with_fallback(config_path, fallback)  # an invalid file must not take the UI down
        self.db_path = Path(cfg.db_path)
        self.status_path = self.db_path.parent / "status.json"
        self.command_dir = commands.command_dir(self.db_path)

    def test_event_blocker(self) -> str | None:
        """Same rules as the service's (which stays authoritative), checked here for an immediate answer."""
        def latest(sql: str) -> float | None:
            rows = self.query(sql)
            t = rows[0][0] if rows else None
            return (datetime.now(UTC) - datetime.fromisoformat(t)).total_seconds() if t else None
        test_age = latest("SELECT MAX(first_seen) FROM events WHERE is_test=1")
        if test_age is not None and test_age < commands.TEST_EVENT_COOLDOWN_S:
            return "cooldown"
        cfg = self.cfg()
        window = (max(d.on_duration_s for d in cfg.devices) if cfg else 300) + 30
        real_age = latest("SELECT MAX(first_seen) FROM events WHERE is_test=0 AND state NOT IN ('ignored_old','abandoned')")
        if real_age is not None and real_age < window:
            return "real_event"
        return None

    def service_alive(self) -> bool:
        st = self.status()
        return bool(st) and time.time() - st["written"] < webpages.STALE_STATUS_S

    # ---- data ----
    def cfg(self) -> AppCfg | None:
        try:
            return load_config(self.config_path)
        except Exception:
            log.warning("cannot load config for the web UI", exc_info=True)
            return None

    def config_error(self) -> str | None:
        try:
            load_config(self.config_path)
            return None
        except Exception as e:
            return str(e)

    def can_restore(self) -> bool:
        return bool(self.fallback and self.fallback.is_file())

    def restore_last_good(self) -> None:
        """Put the service's last-good copy back; the broken file is kept as config.yaml.broken."""
        data = self.fallback.read_bytes()
        load_config(self.fallback)  # never restore something invalid
        try:
            shutil.copy2(self.config_path, self.config_path.with_name(self.config_path.name + ".broken"))
        except OSError:
            pass
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.config_path)

    def status(self) -> dict | None:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        db = None
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            rows = list(db.execute(sql, args))
            self.db_error = None
            return rows
        except sqlite3.Error as e:  # unreadable or corrupt: say so on the page instead of failing it
            log.warning("db query failed: %s", e)
            self.db_error = str(e)
            return []
        finally:
            if db is not None:
                db.close()

    # ---- auth ----
    def new_session(self) -> tuple[str, dict]:
        now = time.monotonic()
        tok = secrets.token_urlsafe(32)
        with self._lock:
            for k in [k for k, v in self.sessions.items() if now - v["seen"] >= SESSION_IDLE_S]:
                del self.sessions[k]
            self.sessions[tok] = {"csrf": secrets.token_urlsafe(24), "seen": now}
            return tok, self.sessions[tok]

    def session(self, token: str | None) -> dict | None:
        with self._lock:
            s = self.sessions.get(token or "")
            if s and time.monotonic() - s["seen"] < SESSION_IDLE_S:
                s["seen"] = time.monotonic()  # idle timeout: activity keeps the session alive
                return s
            self.sessions.pop(token or "", None)
            return None

    def end_session(self, token: str | None) -> None:
        with self._lock:
            self.sessions.pop(token or "", None)

    def locked_out(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self.fails.items() if not v or now - v[-1] >= LOGIN_WINDOW_S]:
                del self.fails[k]  # also keeps the table small
            return len([t for t in self.fails.get(ip, []) if now - t < LOGIN_WINDOW_S]) >= LOGIN_MAX_FAILS

    def check_password(self, ip: str, given: str) -> bool:
        if self.password is None or self.locked_out(ip):
            return False
        if hmac.compare_digest(given.encode(), self.password.encode()):
            with self._lock:
                self.fails.pop(ip, None)
            return True
        with self._lock:
            self.fails.setdefault(ip, []).append(time.monotonic())
        time.sleep(1)  # slow down guessing
        return False


# ---------------------------------------------------------------- HTTP
def make_handler(w: Web):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SHERPA-web"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 20  # seconds a client may take per request; idle keep-alive connections are closed after that

        def log_request(self, code="-", size="-"):  # debug only, path without query string
            log.debug("%s %s %s %s", self.address_string(), self.command, urlsplit(self.path).path, code)

        def log_message(self, fmt, *args):  # timeouts, malformed requests: not worth an INFO line each
            log.debug("%s %s", self.address_string(), fmt % args if args else fmt)

        # -- helpers
        def _cookie(self) -> str | None:
            # By hand: http.cookies gives up on the whole header when another app's cookie is unusual.
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v.strip() or None
            return None

        def _session(self) -> dict | None:
            return w.session(self._cookie())

        def _send(self, status: int, body: str | bytes, ctype="text/html; charset=utf-8", headers: dict | None = None):
            data = body.encode() if isinstance(body, str) else body
            headers = headers or {}
            self.send_response(status)
            if status not in (204, 304):
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
            if "Cache-Control" not in headers:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; img-src 'self'; "
                                                        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in headers.items():
                self.send_header(k, v)
            try:
                self.end_headers()
                if self.command != "HEAD" and status not in (204, 304):
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):  # the client left: nothing to report
                self.close_connection = True

        def _redirect(self, to: str, headers: dict | None = None):
            self._send(HTTPStatus.SEE_OTHER, "", headers={"Location": to, **(headers or {})})

        def _form(self) -> dict[str, str] | None:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                self.close_connection = True  # the unread body must not be parsed as the next request
                self._send(HTTPStatus.LENGTH_REQUIRED, "length required", "text/plain")
                return None
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large", "text/plain")
                return None
            return dict(parse_qsl(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True))

        def _csrf_ok(self, sess: dict | None, form: dict) -> bool:
            return sess is not None and hmac.compare_digest(form.get("csrf", "").encode(), sess["csrf"].encode())

        # -- routes
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            path, q = url.path, dict(parse_qsl(url.query))
            sess = self._session()
            try:
                if path == "/static.css":
                    self._send(200, CSS, "text/css; charset=utf-8", {"Cache-Control": "public, max-age=31536000, immutable"})
                elif path == "/favicon.svg":
                    self._send(200, favicon(q.get("s", "")), "image/svg+xml", {"Cache-Control": "max-age=86400"})
                elif path == "/favicon.ico":
                    self._send(204, "")
                elif path == "/healthz":
                    self._send(200, "ok", "text/plain")
                elif path == "/":
                    self._send(200, webpages.dashboard(w, bool(sess), q))
                elif path == "/help":
                    self._send(200, webpages.help_page(w, bool(sess)))
                elif path == "/events":
                    self._send(200, webpages.events_page(w, bool(sess), q))
                elif path == "/alerts":
                    self._send(200, webpages.alerts_page(w, bool(sess), q))
                elif path == "/stats":
                    self._send(200, webstats.stats_page(w, bool(sess), q))
                elif path == "/events.csv":
                    self._send(200, webexport.events_csv(w), "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="sherpa-events.csv"'})
                elif path == "/api/status":
                    self._send(200, webexport.status_json(w), "application/json")
                elif path == "/metrics":
                    self._send(200, webexport.metrics(w), "text/plain; version=0.0.4; charset=utf-8")
                elif path == "/lights":
                    msg = (("ok", TOOL_DONE[q["done"]]) if q.get("done") in TOOL_DONE else
                           ("bad", TOOL_ERR[q["err"]]) if q.get("err") in TOOL_ERR else None)
                    self._send(200, webpages.lights_page(w, sess, msg))
                elif path == "/settings":
                    if q.get("done") == "saved":
                        self._send(200, webpages.settings_page(w, sess, saved=True))
                    else:
                        msg = (("ok", TOOL_DONE[q["done"]]) if q.get("done") in TOOL_DONE else
                               ("bad", TOOL_ERR[q["err"]]) if q.get("err") in TOOL_ERR else None)
                        self._send(200, webpages.settings_page(w, sess, tool_msg=msg))
                elif path == "/login":
                    expired = "Your session expired, so the last change was not saved. Log in and apply it again."
                    self._send(200, webpages.login_page(w, expired if q.get("expired") else "", disabled=w.password is None,
                                                        notice=bool(q.get("expired"))))
                else:
                    self._send(404, webpages.not_found(w, bool(sess)))
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
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
                        return self._send(200, webpages.login_page(w, disabled=True))
                    ip = self.client_address[0]
                    if w.locked_out(ip):
                        return self._send(429, webpages.login_page(w, "Too many failed attempts. Try again in a few minutes."))
                    if not w.check_password(ip, form.get("password", "")):
                        return self._send(401, webpages.login_page(w, "Wrong password."))
                    tok, _ = w.new_session()
                    log.info("login from %s", ip)
                    return self._redirect("/settings", {"Set-Cookie": f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/"})
                if path == "/logout":
                    w.end_session(self._cookie())  # harmless to force, so no CSRF token needed
                    return self._redirect("/", {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                if path == "/settings":
                    if sess is None:
                        return self._redirect("/login?expired=1")
                    if not self._csrf_ok(sess, form):
                        return self._send(403, "bad CSRF token", "text/plain")
                    errors = settings.save(w.config_path, form, form.get("base", ""))
                    if errors and errors[0] == settings.CONFLICT:  # show the file as it is now, not stale values
                        return self._send(409, webpages.settings_page(w, sess, errors=errors))
                    if errors:
                        return self._send(400, webpages.settings_page(w, sess, errors=errors, form=form))
                    log.warning("settings changed via web from %s", self.client_address[0])
                    return self._redirect("/settings?done=saved")  # a reload must not post again
                if path == "/settings/restore":
                    if sess is None:
                        return self._redirect("/login")
                    if not self._csrf_ok(sess, form):
                        return self._send(403, "bad CSRF token", "text/plain")
                    if not w.can_restore():
                        return self._redirect("/settings?err=no_backup")
                    try:
                        w.restore_last_good()
                    except Exception:
                        log.exception("restoring the last good config failed")
                        return self._redirect("/settings?err=restore_failed")
                    log.warning("last good settings restored via web from %s", self.client_address[0])
                    return self._redirect("/settings?done=restored")
                if path == "/tools/light":
                    if sess is None:
                        return self._redirect("/login")
                    if not self._csrf_ok(sess, form):
                        return self._send(403, "bad CSRF token", "text/plain")
                    return self._redirect(self._light(form))
                if path in ("/tools/telegram-test", "/tools/test-event"):
                    if sess is None:
                        return self._redirect("/login")
                    if not self._csrf_ok(sess, form):
                        return self._send(403, "bad CSRF token", "text/plain")
                    return self._redirect(self._tool(path, form))
                self._send(404, "not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                log.exception("request failed: %s", path)
                self._send(500, "internal error", "text/plain")

        def _light(self, form: dict) -> str:
            """Queue a manual light command: plain/colour ON, an effect, or OFF. ON always has a device-side timer."""
            ip = self.client_address[0]
            if not w.service_alive():
                return "/lights?err=service_down"
            cfg = w.cfg()
            name, action, effect = form.get("device", ""), form.get("action", ""), form.get("effect", "")
            dev = next((d for d in cfg.devices if d.name == name), None) if cfg else None
            if action == "scene":  # the colour & effects form: "solid" is a coloured ON
                action, effect = ("on", "solid") if effect in ("", "solid") else ("effect", effect)
            if dev is None or action not in ("on", "off", "effect"):
                return "/lights?err=light"
            cap = capability_of(dev)
            params: dict = {"device": name, "action": action}
            if action != "off":
                dur = form.get("duration_s", "")
                try:
                    if dur == "hold":
                        params["hold"] = True
                    else:
                        params["duration_s"] = int(dur)
                    if cap == "color" and form.get("color"):
                        params["color"] = form["color"]
                    if cap != "onoff" and form.get("brightness"):
                        params["brightness"] = int(form["brightness"])
                    if action == "effect":
                        if not lights.supports(cap, effect):
                            return "/lights?err=light"
                        params.update(effect=effect, speed=int(form.get("speed", "2")))
                except ValueError:
                    return "/lights?err=light"
            if w.test_event_blocker() == "real_event":  # same window as the service's check, for an instant answer
                return "/lights?err=light_busy"
            if commands.validate({"kind": "light", "ts": time.time(), "source": "cli", **params}) is None:
                return "/lights?err=light"
            try:
                commands.submit(w.command_dir, "light", ip, **params)
            except (OSError, ValueError):
                log.exception("could not queue light command")
                return "/lights?err=write"
            log.warning("light %s %s requested via web from %s", name, action, ip)
            return f"/lights?done=light_{action}"

        def _tool(self, path: str, form: dict) -> str:
            """Queue an admin command for the service; returns where to redirect."""
            ip = self.client_address[0]
            if not w.service_alive():
                return "/settings?err=service_down#tools"
            kind, params = "telegram_test", {}
            if path == "/tools/test-event":
                if form.get("confirm") != "yes":
                    return "/settings?err=confirm#tools"
                try:
                    d = int(form.get("duration_s", ""))
                except ValueError:
                    return "/settings?err=duration#tools"
                if not 1 <= d <= commands.TEST_EVENT_MAX_S:
                    return "/settings?err=duration#tools"
                if blocked := w.test_event_blocker():
                    return f"/settings?err={blocked}#tools"
                kind, params = "test_event", {"duration_s": d}
            try:
                commands.submit(w.command_dir, kind, ip, **params)
            except (OSError, ValueError):
                log.exception("could not queue %s", kind)
                return "/settings?err=write#tools"
            log.warning("%s requested via web from %s %s", kind, ip, params or "")
            return f"/settings?done={kind}#tools"

    return Handler


class _Server(ThreadingHTTPServer):
    """A thread per connection, but at most MAX_CONNECTIONS at once: the web UI shares the VM with the poller,
    and many slow or half-open connections must not starve it. Extra connections get a quick 503."""
    daemon_threads = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(config_path: str, password: str | None, host: str = "0.0.0.0", port: int = 8080,
          fallback: str | None = None) -> ThreadingHTTPServer:
    w = Web(config_path, password, fallback)
    if w.password is None:
        log.warning("SHERPA_WEB_PASSWORD not set: web UI is read-only, settings cannot be changed")
    srv = _Server((host, port), make_handler(w))
    srv.web = w  # type: ignore[attr-defined]
    log.info("web UI v%s on http://%s:%d", BUILD, host, srv.server_address[1])
    return srv
