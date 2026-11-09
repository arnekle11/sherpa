"""Mock feed, mock Shellies (Gen1+Gen2) and mock Telegram for development and self-tests.

Threaded stdlib HTTP servers so they run anywhere with no extra dependencies.
The RSS structure follows the fields observed on the real feed; channel title and
description text are placeholders.
"""
from __future__ import annotations

import json
import threading
import time
from email.utils import format_datetime
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape


class _Server:
    handler_cls: type[BaseHTTPRequestHandler]

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.httpd = ThreadingHTTPServer((host, port), self.handler_cls)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *a: None  # client resets are expected noise
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def hostport(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> "_Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ feed
class _FeedHandler(_Quiet):
    def do_HEAD(self):
        self._send(405, b"")

    def do_GET(self):
        f: MockFeed = self.server.owner  # type: ignore[attr-defined]
        u = urlparse(self.path)
        if u.path != "/feed":
            return self._send(404, b"not found", "text/plain")
        with f.lock:
            mode = f.mode
        if mode == "500":
            return self._send(500, b"boom", "text/plain")
        if mode == "garbage":
            return self._send(200, b"<html><body>maintenance</body></html>", "text/html")
        if mode == "slow":
            time.sleep(10)
        self._send(200, f.render().encode("utf-8"), "text/xml; charset=utf-8")

    def do_POST(self):  # convenience for manual tests: curl -X POST 'http://host:port/inject?title=X'
        f: MockFeed = self.server.owner  # type: ignore[attr-defined]
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/inject":
            guid = f.add_item(q.get("title", "Požar, eksplozija"), q.get("description", "Ljubljana"))
            return self._send(200, json.dumps({"guid": guid}).encode())
        if u.path == "/mode":
            f.mode = q.get("mode", "ok")
            return self._send(200, b"{}")
        self._send(404, b"{}")


class MockFeed(_Server):
    handler_cls = _FeedHandler

    def __init__(self, **kw):
        super().__init__(**kw)
        self.items: list[dict] = []
        self.mode = "ok"  # ok | 500 | garbage | slow
        self.channel_link = "https://spin3.sos112.si/javno/OD/52034"
        self._next_id = 100000

    @property
    def url(self) -> str:
        return f"http://{self.hostport}/feed"

    def add_item(self, title: str, description: str = "Ljubljana", age_s: float = 0,
                 guid: str | None = None) -> str:
        with self.lock:
            self._next_id += 1
            guid = guid or f"https://spin3.sos112.si/javno/zemljevid/{self._next_id}"
            pub = datetime.fromtimestamp(time.time() - age_s, UTC)
            self.items.append({"guid": guid, "title": title, "description": description, "pub": pub})
        return guid

    def set_description(self, guid: str, text: str) -> None:
        with self.lock:
            for it in self.items:
                if it["guid"] == guid:
                    it["description"] = text

    def render(self) -> str:
        with self.lock:
            items = list(reversed(self.items))  # deliberately not time-ordered for tests
            link = self.channel_link
        body = "".join(
            f"<item><title>{escape(i['title'])}</title><description>{escape(i['description'])}</description>"
            f"<link>{escape(i['guid'])}</link><guid>{escape(i['guid'])}</guid>"
            f"<pubDate>{format_datetime(i['pub'], usegmt=True)}</pubDate></item>"
            for i in items)
        return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
                f"<title>SOS 112 (mock)</title><link>{escape(link)}</link>"
                f"<description>mock</description>{body}</channel></rss>")


# ------------------------------------------------------------------ shelly
class _ShellyHandler(_Quiet):
    def do_POST(self):
        s: MockShelly = self.server.owner  # type: ignore[attr-defined]
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, b'{"error":"bad json"}')
        with s.lock:
            s.requests.append((u.path, body))
            if s.dead or s.fail_next > 0:
                if s.fail_next > 0:
                    s.fail_next -= 1
                return self._send(500, b'{"error":"mock failure"}')
            s._expire()
            if s.gen == 2 and u.path == f"/rpc/{s.rpc}.Set":
                s.frames.append(body)
                if "rgb" in body:
                    s.rgb = list(body["rgb"])
                if "brightness" in body:
                    s.brightness = body["brightness"]
                if body.get("on") is True:
                    s._on(float(body["toggle_after"]) if "toggle_after" in body else None)
                    s.on_calls.append(body)
                elif body.get("on") is False:
                    s.is_on, s.timer_expires, s.timer_duration = False, None, None
                return self._send(200, json.dumps({"was_on": False}).encode())
            self._send(404, b'{"error":"not found"}')

    def do_GET(self):
        s: MockShelly = self.server.owner  # type: ignore[attr-defined]
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with s.lock:
            s.requests.append((u.path, q))
            if s.delay_s:
                time.sleep(s.delay_s)
            if s.dead or s.fail_next > 0:
                if s.fail_next > 0:
                    s.fail_next -= 1
                return self._send(500, b'{"error":"mock failure"}')
            s._expire()
            if s.gen == 1 and u.path == "/relay/0":
                if q.get("turn") == "on":
                    s._on(float(q["timer"]) if "timer" in q else None)
                    s.on_calls.append(q)
                elif q.get("turn") == "off":
                    s.is_on, s.timer_expires, s.timer_duration = False, None, None
                return self._send(200, json.dumps(s.gen1_status()).encode())
            if s.gen == 2 and u.path == f"/rpc/{s.rpc}.Set":
                if q.get("on") == "true":
                    s._on(float(q["toggle_after"]) if "toggle_after" in q else None)
                    s.on_calls.append(q)
                elif q.get("on") == "false":
                    s.is_on, s.timer_expires, s.timer_duration = False, None, None
                return self._send(200, json.dumps({"was_on": False}).encode())
            if s.gen == 2 and u.path == f"/rpc/{s.rpc}.GetStatus":
                return self._send(200, json.dumps(s.gen2_status()).encode())
            self._send(404, b'{"error":"not found"}')


class MockShelly(_Server):
    handler_cls = _ShellyHandler

    def __init__(self, gen: int = 1, component: str = "switch", **kw):
        super().__init__(**kw)
        self.gen = gen
        # Gen2 answers only for its own component (an rgb-profile Shelly has no Switch.*), like the real device
        self.rpc = {"switch": "Switch", "light": "Light", "rgb": "RGB", "rgbw": "RGBW"}[component]
        self.is_on = False
        self.timer_duration: float | None = None
        self.timer_expires: float | None = None
        self.requests: list = []
        self.on_calls: list[dict] = []
        self.frames: list[dict] = []          # every POSTed Set, in order (manual scenes)
        self.rgb = [255, 0, 0]
        self.brightness = 8
        self.fail_next = 0
        self.dead = False
        self.delay_s = 0.0

    def _expire(self) -> None:
        if self.timer_expires is not None and time.monotonic() >= self.timer_expires:
            self.is_on, self.timer_expires, self.timer_duration = False, None, None

    def _on(self, timer: float | None) -> None:
        self.is_on = True
        self.timer_duration = timer
        self.timer_expires = time.monotonic() + timer if timer else None

    def _remaining(self) -> float:
        return max(0.0, self.timer_expires - time.monotonic()) if self.timer_expires else 0.0

    def gen1_status(self) -> dict:
        return {"ison": self.is_on, "has_timer": self.timer_expires is not None,
                "timer_duration": int(self.timer_duration or 0),
                "timer_remaining": int(self._remaining()), "source": "http"}

    def gen2_status(self) -> dict:
        d = {"id": 0, "source": "HTTP", "output": self.is_on}
        if self.rpc != "Switch":
            d.update(rgb=list(self.rgb), brightness=self.brightness)
        if self.timer_expires is not None:
            d.update(timer_started_at=time.time(), timer_duration=float(self.timer_duration))
        return d

    def reset(self) -> None:
        with self.lock:
            self.fail_next, self.dead, self.delay_s = 0, False, 0.0


# ------------------------------------------------------------------ telegram
class _TgHandler(_Quiet):
    def do_POST(self):
        t: MockTelegram = self.server.owner  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        with t.lock:
            t.messages.append(payload.get("text", ""))
        self._send(200, b'{"ok":true,"result":{}}')


class MockTelegram(_Server):
    handler_cls = _TgHandler

    def __init__(self, **kw):
        super().__init__(**kw)
        self.messages: list[str] = []

    @property
    def api_base(self) -> str:
        return f"http://{self.hostport}"
