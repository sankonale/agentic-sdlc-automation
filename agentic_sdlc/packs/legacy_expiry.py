"""Skill pack (brownfield recipe): add link expiry + fix hash-collision overwrite
in a `legacy_shortener`-shaped codebase.

A recipe declares *preconditions* on the scanned codebase model. If the target
repository does not match (different symbols), the recipe refuses to apply and
the ChangeImplementer falls back to the LLM provider or fails safely - it never
guesses edits against code it does not recognise.
"""
from __future__ import annotations

PRECONDITIONS = {
    "shortener.core": {"classes": {"Shortener": ["shorten", "resolve", "_hash"]}},
    "shortener.store": {"classes": {"InMemoryStore": ["save", "get"]}},
    "shortener.web": {"classes": {"Handler": ["do_POST", "do_GET"]}},
}


def matches(model: dict) -> list[str]:
    """Return a list of unmet preconditions (empty list == recipe applies)."""
    missing = []
    mods = model["modules"]
    for mod, req in PRECONDITIONS.items():
        if mod not in mods:
            missing.append(f"module {mod}")
            continue
        for cls, methods in req["classes"].items():
            have = {m.split("(")[0] for m in mods[mod]["classes"].get(cls, [])}
            for m in methods:
                if m not in have:
                    missing.append(f"{mod}.{cls}.{m}")
    return missing


STORE = '''"""In-memory storage for short links."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LinkRecord:
    url: str
    expires_at: float | None = None

    def is_expired(self, now):
        return self.expires_at is not None and now >= self.expires_at


class InMemoryStore:
    def __init__(self):
        self._links = {}

    def save(self, code, url, expires_at=None):
        self._links[code] = LinkRecord(url, expires_at)

    def get(self, code):
        """Backward compatible: returns the URL (or None), ignoring expiry."""
        rec = self._links.get(code)
        return rec.url if rec else None

    def get_record(self, code):
        return self._links.get(code)

    def delete(self, code):
        self._links.pop(code, None)

    def __len__(self):
        return len(self._links)
'''

CORE = '''"""Core shortening logic."""
import hashlib
import time

from .store import InMemoryStore, LinkRecord

CODE_LENGTH = 6
MAX_REHASH = 16

ACTIVE, EXPIRED, MISSING = "active", "expired", "missing"


class CodeSpaceError(RuntimeError):
    """No free code could be found within MAX_REHASH attempts."""


class Shortener:
    def __init__(self, store=None, clock=time.time):
        self.store = store or InMemoryStore()
        self.clock = clock

    def _hash(self, url, attempt=0):
        material = url if attempt == 0 else f"{url}#{attempt}"
        return hashlib.sha256(material.encode()).hexdigest()[:CODE_LENGTH]

    def _record(self, code):
        getter = getattr(self.store, "get_record", None)
        if getter is not None:
            return getter(code)
        url = self.store.get(code)          # legacy custom store without expiry support
        return LinkRecord(url) if url is not None else None

    def shorten(self, url, ttl_seconds=None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if ttl_seconds is not None:
            if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
                raise ValueError("ttl_seconds must be a positive number")
            if not hasattr(self.store, "get_record"):
                raise ValueError("configured store does not support expiry")
        now = self.clock()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        for attempt in range(MAX_REHASH):
            code = self._hash(url, attempt)
            rec = self._record(code)
            if rec is None or rec.is_expired(now):
                if expires_at is None:
                    self.store.save(code, url)
                else:
                    self.store.save(code, url, expires_at)
                return code
            if rec.url == url:
                # Idempotent: an active link for the same URL keeps its code and expiry.
                return code
            # Collision with a different, still-active URL: never overwrite, re-hash.
        raise CodeSpaceError(f"no free code for {url!r} after {MAX_REHASH} attempts")

    def status(self, code):
        rec = self._record(code)
        if rec is None:
            return MISSING
        return EXPIRED if rec.is_expired(self.clock()) else ACTIVE

    def resolve(self, code):
        rec = self._record(code)
        if rec is None or rec.is_expired(self.clock()):
            return None
        return rec.url
'''

WEB = '''"""Minimal HTTP front-end (stdlib only)."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import EXPIRED, Shortener

shortener = Shortener()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/shorten":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return
        try:
            code = shortener.shorten(body.get("url", ""), ttl_seconds=body.get("ttl_seconds"))
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        payload = json.dumps({"code": code}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        code = self.path.lstrip("/")
        url = shortener.resolve(code)
        if url is None:
            self.send_error(410 if shortener.status(code) == EXPIRED else 404)
            return
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def log_message(self, fmt, *args):  # keep test output clean
        pass


def main(port=8080):
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''

FILES = {"shortener/store.py": STORE, "shortener/core.py": CORE, "shortener/web.py": WEB}

# (file, correct, defective) - used by --inject-fault defect:impl_change
DEFECT = ("shortener/core.py",
          "        if rec is None or rec.is_expired(self.clock()):\n            return None\n        return rec.url",
          "        if rec is None:\n            return None\n        return rec.url")

EXPIRY_TEST = '''# covers: CR-1, CR-2
"""Tests added by agentic-sdlc for link expiry and the collision-overwrite bug."""
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from shortener import web
from shortener.core import ACTIVE, EXPIRED, MISSING, Shortener


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


# --- expiry -------------------------------------------------------------------------
def test_link_resolves_until_ttl_then_expires(clock):
    s = Shortener(clock=clock)
    code = s.shorten("https://example.com/promo", ttl_seconds=60)
    clock.t += 59
    assert s.resolve(code) == "https://example.com/promo"
    clock.t += 1
    assert s.resolve(code) is None
    assert s.status(code) == EXPIRED


def test_links_without_ttl_never_expire(clock):
    s = Shortener(clock=clock)
    code = s.shorten("https://example.com/forever")
    clock.t += 10 ** 9
    assert s.resolve(code) == "https://example.com/forever"
    assert s.status(code) == ACTIVE


@pytest.mark.parametrize("bad", [0, -5, "60", True])
def test_invalid_ttl_rejected(bad):
    with pytest.raises(ValueError):
        Shortener().shorten("https://example.com", ttl_seconds=bad)


def test_status_missing():
    assert Shortener().status("zzzzzz") == MISSING


def test_expired_code_can_be_reissued(clock):
    s = Shortener(clock=clock)
    code = s.shorten("https://example.com/a", ttl_seconds=10)
    clock.t += 11
    assert s.shorten("https://example.com/a", ttl_seconds=10) == code
    assert s.resolve(code) == "https://example.com/a"


# --- collision bug fix ---------------------------------------------------------------
from tests.test_bug_collision_repro import _force_collisions  # noqa: E402


def test_idempotency_preserved_under_collision(monkeypatch):
    _force_collisions(monkeypatch)
    s = Shortener()
    s.shorten("https://a.example")
    b1 = s.shorten("https://b.example")
    assert s.shorten("https://b.example") == b1


def test_legacy_custom_store_still_supported():
    class DictStore:
        def __init__(self):
            self.d = {}

        def save(self, code, url):
            self.d[code] = url

        def get(self, code):
            return self.d.get(code)

    s = Shortener(DictStore())
    code = s.shorten("https://legacy.example")
    assert s.resolve(code) == "https://legacy.example"
    with pytest.raises(ValueError):
        s.shorten("https://legacy.example/2", ttl_seconds=5)


# --- HTTP layer ----------------------------------------------------------------------
@pytest.fixture
def server(monkeypatch, clock):
    monkeypatch.setattr(web, "shortener", Shortener(clock=clock))
    httpd = HTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(url):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(url).status
    except urllib.error.HTTPError as e:
        return e.code


def _post(base, body):
    req = urllib.request.Request(base + "/shorten", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_http_expired_link_returns_410(server, clock):
    status, body = _post(server, {"url": "https://example.com/x", "ttl_seconds": 30})
    assert status == 201
    assert _get(f"{server}/{body['code']}") == 302
    clock.t += 31
    assert _get(f"{server}/{body['code']}") == 410
    assert _get(f"{server}/unknown") == 404


def test_http_rejects_bad_ttl(server):
    status, _ = _post(server, {"url": "https://example.com", "ttl_seconds": -1})
    assert status == 400
'''

REPRO_TEST = '''# covers: CR-2
"""Bug reproduction: uses only the v1 public API, so it runs against the baseline
(where it must FAIL, proving the bug) and against the patched code (must PASS)."""
from shortener.core import Shortener


def _force_collisions(monkeypatch):
    real = Shortener._hash

    def colliding(self, url, attempt=0):
        return "aaaaaa" if attempt == 0 else real(self, url, attempt)

    monkeypatch.setattr(Shortener, "_hash", colliding)


def test_collision_does_not_overwrite_existing_link(monkeypatch):
    _force_collisions(monkeypatch)
    s = Shortener()
    a = s.shorten("https://a.example")
    b = s.shorten("https://b.example")
    assert a != b
    assert s.resolve(a) == "https://a.example"
    assert s.resolve(b) == "https://b.example"
'''

REPRO_TEST_PATH = "tests/test_bug_collision_repro.py"
NEW_TESTS = {"tests/test_expiry_and_collisions.py": EXPIRY_TEST, REPRO_TEST_PATH: REPRO_TEST}
