"""Database connection: SQLite locally, PostgreSQL in production.

Models bind to the `db` proxy at import time; `configure_database()` resolves it to a real
backend at startup, so nothing below has to know which engine it is talking to.
"""
import logging
import re
import threading

from peewee import (
    DatabaseProxy,
    InterfaceError,
    OperationalError,
    PostgresqlDatabase,
    SqliteDatabase,
)
from playhouse.shortcuts import ReconnectMixin

import config

log = logging.getLogger(__name__)

db = DatabaseProxy()

# SQLite needs these to behave like a real database: enforce foreign keys and use WAL so a
# reader doesn't block a writer.
_SQLITE_PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "cache_size": -32 * 1000,
    "synchronous": 1,
}


class DatabaseNotConfigured(RuntimeError):
    """Raised when the Postgres backend is selected but .env has no real credentials."""


_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchemaPostgresqlDatabase(ReconnectMixin, PostgresqlDatabase):
    """Postgres connection that survives a dropped link and sets its own `search_path`.

    **Reconnect.** Warehouse PCs talk to Sydney over the public internet, so the
    connection will drop — a dozing laptop, a flapping router, a pooler restart. Without
    this the next query raises and the user has to restart the app. ReconnectMixin
    retries once after re-establishing the connection, but only outside a transaction:
    replaying half a transaction would be far worse than an error.

    **search_path.** Set with a statement after connecting rather than via the `options`
    startup parameter, because Supabase's Supavisor pooler reads `options` to work out
    which tenant you are. Anything else in there makes it fall back to a bare `postgres`
    user and reject the login with a misleading "password authentication failed".
    """

    # Matched against the exception text; these are the psycopg2 wordings for a link
    # that has gone away, plus the transient DNS failure seen against the pooler.
    reconnect_errors = (
        (OperationalError, "server closed the connection"),
        (OperationalError, "connection already closed"),
        (OperationalError, "could not receive data from server"),
        (OperationalError, "could not send data to server"),
        (OperationalError, "SSL connection has been closed"),
        (OperationalError, "terminating connection"),
        (OperationalError, "could not translate host name"),
        (OperationalError, "Name or service not known"),
        (OperationalError, "connection has been closed unexpectedly"),
        (InterfaceError, "connection already closed"),
        (InterfaceError, "cursor already closed"),
    )

    def __init__(self, *args, schema="public", **kwargs):
        self.schema = schema or "public"
        if not _SAFE_SCHEMA.match(self.schema):
            raise DatabaseNotConfigured(
                f"DB_SCHEMA '{self.schema}' is not a valid schema name."
            )
        # The active schema is per-thread: the web server serves different tenants on
        # different threads, and a reconnect must restore *this* thread's tenant schema,
        # not a global one. Defaults to self.schema until a request picks a tenant.
        self._active = threading.local()
        super().__init__(*args, **kwargs)

    def active_schema(self) -> str:
        return getattr(self._active, "schema", None) or self.schema

    def set_active_schema(self, schema):
        """Switch this thread's tenant schema and remember it across reconnects.

        A dropped link (routine over the warehouse->Sydney hop) triggers ReconnectMixin,
        which re-runs _initialize_connection. If that pinned search_path to the fixed
        default, the rest of an in-flight tenant request would silently run against the
        wrong schema. Recording the choice per thread lets the reconnect restore it.
        """
        if not _SAFE_SCHEMA.match(schema or ""):
            raise DatabaseNotConfigured(f"Refusing suspicious schema name '{schema}'.")
        self._active.schema = schema
        self.execute_sql(f'SET search_path TO "{schema}"')

    def _initialize_connection(self, conn):
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{self.active_schema()}"')
        conn.commit()


def build_database():
    """Create the concrete peewee database for the configured backend."""
    if config.DB_BACKEND == "postgres":
        if not config.PG_CONFIGURED:
            raise DatabaseNotConfigured(
                "DB_BACKEND=postgres but DB_HOST in .env is missing or still the "
                "placeholder 'db.YOUR_PROJECT_REF.supabase.co'. Fill in your Supabase "
                "connection details, or set DB_BACKEND=sqlite to work locally."
            )
        params = dict(
            schema=config.PG_SCHEMA,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            host=config.PG_HOST,
            port=config.PG_PORT,
            sslmode=config.PG_SSLMODE,
            connect_timeout=15,
            autorollback=True,
        )
        if config.PG_HAS_ROOTCERT:
            params["sslrootcert"] = config.PG_SSLROOTCERT
            log.info("TLS: verifying the server certificate against %s",
                     config.PG_SSLROOTCERT)
        elif config.PG_SSLMODE in ("require", "prefer", "allow"):
            log.warning(
                "TLS: the connection is encrypted but the server certificate is NOT "
                "verified. Add the Supabase CA to certs/ and set DB_SSLROOTCERT to "
                "close off machine-in-the-middle attacks.")
        return SchemaPostgresqlDatabase(config.PG_NAME, **params)

    if config.DB_BACKEND == "sqlite":
        return SqliteDatabase(str(config.SQLITE_PATH), pragmas=_SQLITE_PRAGMAS)

    raise DatabaseNotConfigured(
        f"Unknown DB_BACKEND '{config.DB_BACKEND}'. Use 'sqlite' or 'postgres'."
    )


def configure_database():
    """Point the proxy at a real database. Safe to call once at startup."""
    real_db = build_database()
    db.initialize(real_db)
    return real_db


def connect(reuse: bool = True):
    """Open the connection, translating driver errors into something actionable."""
    try:
        db.connect(reuse_if_open=reuse)
    except OperationalError as exc:
        raise DatabaseNotConfigured(
            f"Could not reach the {config.DB_BACKEND} database: {exc}"
        ) from exc
    return db


def close():
    if not db.is_closed():
        db.close()


def is_postgres() -> bool:
    return config.DB_BACKEND == "postgres"
