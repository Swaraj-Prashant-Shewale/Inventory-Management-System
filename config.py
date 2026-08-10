"""Application configuration, loaded from .env with sensible defaults.

The app runs against SQLite locally and PostgreSQL (Supabase) in production; the only
thing that changes between them is DB_BACKEND in .env.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def app_root() -> Path:
    """Directory the app treats as its home.

    Under PyInstaller the bundle is extracted to a temp dir, so writable files and .env
    must live next to the .exe rather than inside the bundle.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


ROOT = app_root()
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Database -----------------------------------------------------------------------
# "sqlite" for local development, "postgres" for the shared cloud database.
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").strip().lower()

SQLITE_PATH = ROOT / os.getenv("SQLITE_FILE", "inventory_dev.db")

PG_NAME = os.getenv("DB_NAME", "postgres")
PG_USER = os.getenv("DB_USER", "postgres")
PG_PASSWORD = os.getenv("DB_PASSWORD", "")
PG_HOST = os.getenv("DB_HOST", "")
PG_PORT = int(os.getenv("DB_PORT") or 5432)

# Supabase's pooler presents a certificate signed by its own CA, so verifying it needs
# that CA file (Supabase dashboard → Project Settings → Database → SSL configuration).
# Drop it in and verification turns on by itself; without it the link is still encrypted
# but the server's identity is unverified, which leaves an active MITM possible.
PG_SSLROOTCERT = os.getenv("DB_SSLROOTCERT", "").strip()
if PG_SSLROOTCERT and not os.path.isabs(PG_SSLROOTCERT):
    PG_SSLROOTCERT = str(ROOT / PG_SSLROOTCERT)
PG_HAS_ROOTCERT = bool(PG_SSLROOTCERT) and os.path.exists(PG_SSLROOTCERT)

_requested_sslmode = os.getenv("DB_SSLMODE", "require").strip() or "require"
if PG_HAS_ROOTCERT and _requested_sslmode in ("require", "prefer", "allow"):
    PG_SSLMODE = "verify-full"      # a usable CA is present, so actually verify
else:
    PG_SSLMODE = _requested_sslmode
# Lets tests run against a scratch schema on the same database as live data.
PG_SCHEMA = os.getenv("DB_SCHEMA", "public").strip() or "public"

# Placeholder host shipped in the original .env — treated as "not configured yet".
PG_CONFIGURED = bool(PG_HOST) and "YOUR_PROJECT_REF" not in PG_HOST

# --- Security -----------------------------------------------------------------------
# OWASP's current floor for PBKDF2-HMAC-SHA256. Measured at ~260 ms per verify on a
# typical warehouse PC — paid once at sign-in. Stored hashes record the count they were
# made with, so raising this stays backward compatible and old hashes upgrade on login.
PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS") or 600_000)

# Lock a session after this many idle minutes. Warehouse terminals are shared and often
# left signed in. 0 disables the lock.
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES") or 20)

# --- Business defaults --------------------------------------------------------------
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "₹")
FINANCIAL_YEAR_START_MONTH = int(os.getenv("FY_START_MONTH") or 4)  # April
DEFAULT_GST_RATE = float(os.getenv("DEFAULT_GST_RATE") or 18.0)

# --- Paths --------------------------------------------------------------------------
UI_DIR = Path(__file__).resolve().parent / "ui"
STYLES_PATH = UI_DIR / "styles.qss"
DOCUMENTS_DIR = ROOT / os.getenv("DOCUMENTS_DIR", "documents")
LOG_PATH = ROOT / "app.log"

DEBUG = _bool("DEBUG", False)
