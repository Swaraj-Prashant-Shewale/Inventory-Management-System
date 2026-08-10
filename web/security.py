"""Web session and CSRF machinery.

Sessions are a signed, timestamped cookie holding only identifiers — nothing sensitive
lives client-side, and tampering breaks the signature. The idle timeout comes from the
cookie's max-age, refreshed on every response, which is the web equivalent of the
desktop idle lock. A per-session random CSRF token rides inside the signed payload and
must be echoed by every state-changing form.
"""
import logging
import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config

log = logging.getLogger(__name__)

SESSION_COOKIE = "ims_session"
ADMIN_COOKIE = "ims_admin"
# Pre-session anti-CSRF token for the login forms (there is no session yet to carry one).
LOGIN_CSRF_COOKIE = "ims_lcsrf"
LOGIN_CSRF_MAX_AGE = 1800

# The desktop default of 20 idle minutes is aggressive for the web, where closing the
# lid mid-task is routine. 60 is still short enough for a shared terminal.
SESSION_IDLE_SECONDS = max(config.SESSION_IDLE_MINUTES, 60) * 60


def _secret_key() -> str:
    """A stable signing key: env var in production, generated file for development.

    Restarting with a new key would sign everyone out, so the generated key persists. The
    create is atomic (O_CREAT|O_EXCL) and the value is always read back from disk, so if
    several workers start at once on a fresh deploy they all converge on one key instead
    of each memoizing a different one and rejecting each other's cookies.
    """
    from_env = os.environ.get("WEB_SECRET_KEY", "").strip()
    if from_env:
        return from_env

    key_file = config.ROOT / "web_secret.key"
    if not key_file.exists():
        try:
            fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
            log.info("Generated a new web session signing key at %s", key_file)
        except FileExistsError:
            pass   # another worker created it first — fall through and read theirs
    return key_file.read_text(encoding="ascii").strip()


_serializer = None


def serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_secret_key(), salt="ims-session")
    return _serializer


def issue_session(user_id, tenant_id) -> str:
    """A fresh signed session for a tenant user."""
    return serializer().dumps({
        "kind": "user",
        "u": user_id,
        "t": tenant_id,
        "csrf": secrets.token_urlsafe(24),
    })


def issue_admin_session(admin_id) -> str:
    return serializer().dumps({
        "kind": "admin",
        "a": admin_id,
        "csrf": secrets.token_urlsafe(24),
    })


def read_session(cookie_value, expected_kind="user"):
    """Decode and verify a session cookie. None if absent, expired or tampered."""
    if not cookie_value:
        return None
    try:
        payload = serializer().loads(cookie_value, max_age=SESSION_IDLE_SECONDS)
    except SignatureExpired:
        return None
    except BadSignature:
        log.warning("Rejected a session cookie with a bad signature")
        return None
    if payload.get("kind") != expected_kind:
        return None
    return payload


def refresh(payload) -> str:
    """Re-sign the same payload with a fresh timestamp — the sliding idle window."""
    return serializer().dumps(payload)


def csrf_matches(payload, submitted) -> bool:
    expected = (payload or {}).get("csrf") or ""
    return bool(expected) and secrets.compare_digest(expected, submitted or "")


def issue_login_csrf() -> str:
    """A fresh signed token for a login form (double-submit against login CSRF)."""
    return serializer().dumps({"kind": "login_csrf", "n": secrets.token_urlsafe(18)})


def login_csrf_valid(cookie_value, submitted) -> bool:
    """The login form's hidden token must equal the value in its own signed cookie.

    A cross-site forced login can't read or set this SameSite=Lax cookie, so the two can
    never match on a forged request. A same-site login always carries both.
    """
    if not cookie_value or not submitted:
        return False
    if not secrets.compare_digest(cookie_value, submitted):
        return False
    try:
        payload = serializer().loads(cookie_value, max_age=LOGIN_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return payload.get("kind") == "login_csrf"


# HTTPS-only cookies. Default ON in production (Postgres backend), where the site is
# served over HTTPS, so a forgotten env var cannot ship auth cookies in cleartext; OFF
# for local SQLite dev over http. An explicit WEB_COOKIE_SECURE always wins.
def _cookie_secure_default() -> bool:
    import os
    raw = os.environ.get("WEB_COOKIE_SECURE", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return config.DB_BACKEND == "postgres"


COOKIE_SECURE = _cookie_secure_default()


def set_cookie(response, name, value, max_age=None):
    response.set_cookie(
        name, value,
        max_age=SESSION_IDLE_SECONDS if max_age is None else max_age,
        httponly=True,               # no script access
        samesite="lax",              # blocks cross-site sends on unsafe methods
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_cookie(response, name):
    response.delete_cookie(name, path="/")
