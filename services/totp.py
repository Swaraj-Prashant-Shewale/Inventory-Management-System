"""Time-based one-time passwords (RFC 6238) using only the standard library.

Same rationale as the PBKDF2 password hashing in auth.py: no compiled dependency keeps the
build portable. The defaults (SHA1, 6 digits, 30-second step) are what Google Authenticator,
Authy, Microsoft Authenticator and 1Password all assume, so a generated secret scans and
verifies without any per-app configuration.
"""
import base64
import hashlib
import hmac
import os
import struct
import time
import urllib.parse

DIGITS = 6
PERIOD = 30


def generate_secret(length: int = 20) -> str:
    """A fresh base32 secret (unpadded) to hand to an authenticator app."""
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def _pad(secret: str) -> str:
    return secret + "=" * (-len(secret) % 8)


def _code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(_pad(secret.upper()))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def now_code(secret: str, at: float = None) -> str:
    """The current code for a secret (used by the enrolment confirmation and tests)."""
    at = time.time() if at is None else at
    return _code_at(secret, int(at // PERIOD))


def verify(secret: str, code: str, window: int = 1, at: float = None) -> bool:
    """True if `code` matches the current step, allowing ±`window` steps for clock skew."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    # isascii() guards against non-ASCII Unicode numerals (e.g. Arabic-Indic digits), which
    # str.isdigit() accepts but hmac.compare_digest then rejects with a TypeError.
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return False
    at = time.time() if at is None else at
    counter = int(at // PERIOD)
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_code_at(secret, counter + delta), code):
            return True
    return False


def provisioning_uri(secret: str, account: str, issuer: str = "Inventory Admin") -> str:
    """The otpauth:// URL an authenticator app turns into a QR / manual entry."""
    label = urllib.parse.quote(f"{issuer}:{account}")
    params = urllib.parse.urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": PERIOD,
    })
    return f"otpauth://totp/{label}?{params}"
