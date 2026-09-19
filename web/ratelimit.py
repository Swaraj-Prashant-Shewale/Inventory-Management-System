"""In-process rate limiting for the login endpoints.

The documented production deployment is a single uvicorn worker (see run_web.py), so a
plain in-memory limiter keyed by client IP is correct here — and consistent with the
alert-refresh throttle in routes_main. It exists to blunt two attacks the login form
invites:

* **Password spraying** — one guess against many accounts. The per-account lockout in
  auth.py never trips because each account sees only one attempt; an IP limit does.
* **CPU exhaustion** — every login attempt costs a ~0.5s PBKDF2 verification (600k
  iterations). An unmetered form lets one client pin the single worker's CPU and take the
  whole site offline with a plain loop. Once an IP is over its limit we answer 429 *before*
  doing any PBKDF2 work, so the abuse costs the server almost nothing.

A successful login resets the caller's counter, so a busy office behind one shared (NAT) IP
is effectively never limited — only repeated *failures* accumulate.
"""
import os
import threading
import time

import config


def _trust_proxy_default() -> bool:
    """Whether to believe the X-Forwarded-For header for the client IP.

    On for the postgres backend (production sits behind an HTTPS reverse proxy that sets
    the header), off for local http/SQLite dev (where the header would be attacker-supplied
    and request.client.host is the real peer). An explicit WEB_TRUST_PROXY always wins.
    Mirrors the cookie-secure default in web/security.py.
    """
    raw = os.environ.get("WEB_TRUST_PROXY", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return config.DB_BACKEND == "postgres"


TRUST_PROXY = _trust_proxy_default()


def _trusted_hops() -> int:
    """How many trusted reverse proxies sit in front of the app (default 1).

    A single HTTPS front (Render / nginx / Caddy) is one hop; putting Cloudflare in front
    of that makes it two. Set WEB_TRUSTED_PROXY_HOPS=2 in that case.
    """
    try:
        return max(1, int(os.environ.get("WEB_TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        return 1


TRUSTED_HOPS = _trusted_hops()


def client_ip(request) -> str:
    """Best-effort client IP used only as a rate-limit key.

    X-Forwarded-For arrives as `<client-supplied…>, <peer seen by proxy 1>, …`. Each
    trusted proxy APPENDS the address it received the connection from, so only the entries
    our own proxies added — counting in from the RIGHT — are trustworthy. The leftmost
    entries are whatever the client chose to send and must never key the limiter, or an
    attacker rotates a fake left value per request and bypasses throttling entirely.
    """
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= TRUSTED_HOPS:
            return parts[-TRUSTED_HOPS]
    client = request.client
    return client.host if client else "unknown"


class RateLimiter:
    """Sliding window: at most `max_events` per `window` seconds per key."""

    def __init__(self, max_events, window, sweep_at=4096):
        self.max_events = max_events
        self.window = window
        self._sweep_at = sweep_at
        self._hits = {}
        self._lock = threading.Lock()

    @staticmethod
    def _prune(times, cutoff):
        i = 0
        n = len(times)
        while i < n and times[i] <= cutoff:
            i += 1
        if i:
            del times[:i]

    def retry_after(self, key) -> int:
        """0 if another event is allowed now (and records it), else seconds to wait."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            if len(self._hits) > self._sweep_at:
                self._sweep(cutoff)
            times = self._hits.get(key)
            if times is None:
                times = []
                self._hits[key] = times
            self._prune(times, cutoff)
            if len(times) >= self.max_events:
                return max(1, int(times[0] + self.window - now) + 1)
            times.append(now)
            return 0

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)

    def _sweep(self, cutoff):
        """Drop keys whose most recent event has aged out — bounds memory under a flood
        of distinct (possibly spoofed) IPs."""
        dead = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for k in dead:
            self._hits.pop(k, None)


# Tuned so honest use never trips (successful logins reset the counter) while spraying and
# CPU abuse do. Admin is a single operator, so it is held tighter.
LOGIN_LIMITER = RateLimiter(max_events=20, window=300)
ADMIN_LOGIN_LIMITER = RateLimiter(max_events=10, window=300)
