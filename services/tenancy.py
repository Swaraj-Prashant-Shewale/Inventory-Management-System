"""Multi-tenant machinery: one website, one database, one schema per client.

Each client company lives in its own Postgres schema (`t_<slug>`) holding the full
41-table business model — their own items, orders, invoice numbering and GST settings.
The `public` schema holds only the platform registry: which tenants exist, which
username belongs to which tenant, and the platform administrator accounts.

Isolation is structural rather than per-query: a request sets `search_path` to its
tenant's schema before touching business data, so there is no tenant_id to forget on a
WHERE clause. The registry tables are always addressed schema-qualified, so they resolve
regardless of the current search_path.

On SQLite (local development) there are no schemas, so the app runs single-tenant with a
stub default tenant and this module mostly stands down.
"""
import datetime
import logging
import re
import secrets
import string
from dataclasses import dataclass

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
)

import config
from database.connection import db, is_postgres

log = logging.getLogger(__name__)

APP_ROLE = "inventory_app"
SCHEMA_PREFIX = "t_"
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,38}$")

# Columns that stay writable on the frozen ledgers so ON DELETE SET NULL works.
# Mirrors security_setup.py, which applies the same freeze to the public schema.
AUDIT_NULLABLE_FKS = ["user_id"]
MOVEMENT_NULLABLE_FKS = ["lot_id", "user_id", "employee_id", "supplier_id",
                         "customer_id"]


class TenancyError(Exception):
    """A platform operation was refused. Safe to show the user."""


# --- Registry models (public schema) ------------------------------------------------

class _RegistryModel(Model):
    class Meta:
        database = db
        legacy_table_names = False
        # Pin to public so these resolve no matter which tenant schema is active.
        schema = "public" if config.DB_BACKEND == "postgres" else None


class Tenant(_RegistryModel):
    slug = CharField(max_length=40, unique=True)
    display_name = CharField(max_length=160)
    schema_name = CharField(max_length=48, unique=True)
    is_active = BooleanField(default=True)
    created_at = DateTimeField(default=datetime.datetime.now)
    notes = CharField(max_length=300, null=True)

    def __str__(self):
        return self.display_name


class UserDirectory(_RegistryModel):
    """Platform-wide username → tenant map. Enforces global username uniqueness,
    which is what lets one login page serve every client."""
    username_lower = CharField(max_length=60, unique=True)
    tenant = ForeignKeyField(Tenant, backref="directory_entries", on_delete="CASCADE")
    created_at = DateTimeField(default=datetime.datetime.now)


class PlatformAdmin(_RegistryModel):
    """Operator accounts for the /admin panel. Separate from any tenant's users."""
    username = CharField(max_length=60, unique=True)
    password_hash = CharField(max_length=255)
    full_name = CharField(max_length=120)
    is_active = BooleanField(default=True)
    failed_attempts = IntegerField(default=0)
    locked_until = DateTimeField(null=True)
    last_login_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.datetime.now)


REGISTRY_MODELS = [Tenant, UserDirectory, PlatformAdmin]


# --- Mode and context ---------------------------------------------------------------

def is_multitenant() -> bool:
    return is_postgres()


@dataclass
class DefaultTenant:
    """Stand-in tenant for single-tenant SQLite development."""
    id: int = 0
    slug: str = "default"
    display_name: str = "Development"
    schema_name: str = "main"
    is_active: bool = True


def use_tenant(tenant):
    """Point this connection's search_path at the tenant's schema.

    Must run at the start of every request, because pooled connections keep their
    search_path across requests and the previous request may have served a different
    tenant.
    """
    if not is_multitenant():
        return
    schema = tenant.schema_name
    if not _SLUG_PATTERN.match(schema.removeprefix(SCHEMA_PREFIX)) \
            and not re.match(r"^[a-z][a-z0-9_]{1,47}$", schema):
        raise TenancyError(f"Refusing suspicious schema name '{schema}'.")
    # Route through the connection so the choice survives a mid-request reconnect
    # (see SchemaPostgresqlDatabase.set_active_schema).
    db.set_active_schema(schema)


def find_tenant_for_username(username) -> "Tenant | None":
    """Which client does this username belong to? None if unknown."""
    if not is_multitenant():
        return DefaultTenant()
    entry = (UserDirectory
             .select(UserDirectory, Tenant)
             .join(Tenant)
             .where(UserDirectory.username_lower == (username or "").strip().lower())
             .first())
    if entry is None or not entry.tenant.is_active:
        return None
    return entry.tenant


def get_tenant(tenant_id):
    if not is_multitenant():
        return DefaultTenant()
    tenant = Tenant.get_or_none(Tenant.id == tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    return tenant


def current_tenant_from_search_path():
    """The tenant whose schema is currently active, from the live search_path.

    Lets code deep in the services layer (which only knows it is inside *a* tenant)
    discover *which* tenant, without threading it through every call.
    """
    if not is_multitenant():
        return DefaultTenant()
    row = db.execute_sql("SHOW search_path").fetchone()
    if not row:
        return None
    first = row[0].split(",")[0].strip().strip('"')
    if not first.startswith(SCHEMA_PREFIX):
        return None
    return Tenant.get_or_none(Tenant.schema_name == first)


def register_username(username, tenant):
    """Claim a username platform-wide. Called wherever a tenant user is created."""
    if not is_multitenant():
        return
    key = (username or "").strip().lower()
    if not key:
        raise TenancyError("Username cannot be empty.")
    existing = UserDirectory.get_or_none(UserDirectory.username_lower == key)
    if existing is not None:
        if existing.tenant_id == getattr(tenant, "id", None):
            return existing
        raise TenancyError(
            f"The username '{username}' is already taken on this platform. "
            f"Usernames are unique across all companies — pick another."
        )
    return UserDirectory.create(username_lower=key, tenant=tenant)


def release_username(username):
    if not is_multitenant():
        return
    UserDirectory.delete().where(
        UserDirectory.username_lower == (username or "").strip().lower()
    ).execute()


# --- Provisioning (runs with administrator credentials, never as the app role) ------

def valid_slug(slug) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_PATTERN.match(slug):
        raise TenancyError(
            "Slug must be 2-39 characters: lowercase letters, digits and underscores, "
            "starting with a letter (e.g. 'acme_traders').")
    return slug


def temp_password(length=12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def grant_app_access(cursor, schema, role=APP_ROLE):
    """Give the app role day-to-day rights in a tenant schema — and nothing more.

    Same shape as security_setup.py applies to public: full DML, no DDL, and the audit
    and stock-movement ledgers frozen except for their nullable foreign keys.
    """
    statements = [
        f'GRANT USAGE ON SCHEMA "{schema}" TO {role}',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
        f'TO {role}',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {role}',
        # Only SELECT, INSERT by default so recreating an append-only ledger cannot
        # silently restore write/delete to the app role (mirrors security_setup.py).
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" '
        f'GRANT SELECT, INSERT ON TABLES TO {role}',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" '
        f'GRANT USAGE, SELECT ON SEQUENCES TO {role}',
        f'REVOKE UPDATE, DELETE ON "{schema}".audit_log FROM {role}',
        f'GRANT UPDATE ({", ".join(AUDIT_NULLABLE_FKS)}) ON "{schema}".audit_log '
        f'TO {role}',
        # DELETE revoked too — the ledger must be un-rewritable, not merely un-updatable.
        f'REVOKE UPDATE, DELETE ON "{schema}".stock_movement FROM {role}',
        f'GRANT UPDATE ({", ".join(MOVEMENT_NULLABLE_FKS)}) '
        f'ON "{schema}".stock_movement TO {role}',
    ]
    for statement in statements:
        cursor.execute(statement)


def ensure_registry(admin_connection):
    """Create the public-schema registry tables and grant the app role access.

    Runs as the administrator (table owner must not be the app role, or the app could
    alter the registry). Idempotent.
    """
    cursor = admin_connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.tenant (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(40) NOT NULL UNIQUE,
            display_name VARCHAR(160) NOT NULL,
            schema_name VARCHAR(48) NOT NULL UNIQUE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            notes VARCHAR(300)
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.user_directory (
            id SERIAL PRIMARY KEY,
            username_lower VARCHAR(60) NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL
                REFERENCES public.tenant (id) ON DELETE CASCADE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.platform_admin (
            id SERIAL PRIMARY KEY,
            username VARCHAR(60) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            full_name VARCHAR(120) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMP,
            last_login_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
    for statement in (
        # The registry (which username -> which schema) is admin-owned and read-only to
        # the app. The app only ever reads public.tenant; all writes go through the admin
        # connection in provision_tenant/drop_tenant. Granting it write here would let a
        # chained bug re-point a tenant's schema_name and cross the tenant boundary.
        f"GRANT SELECT ON public.tenant TO {APP_ROLE}",
        f"GRANT SELECT, INSERT, DELETE ON public.user_directory TO {APP_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        # The app authenticates admins, so it must READ platform_admin and update the
        # login-tracking columns — but never create, delete or re-hash one. Minting or
        # tampering with an operator account stays an administrator-only act.
        f"GRANT SELECT ON public.platform_admin TO {APP_ROLE}",
        f"GRANT UPDATE (failed_attempts, locked_until, last_login_at) "
        f"ON public.platform_admin TO {APP_ROLE}",
    ):
        cursor.execute(statement)


def provision_tenant(admin_connection, admin_password, slug, display_name,
                     owner_username, owner_full_name, owner_password=None,
                     notes=None, warehouse_name="Main Warehouse",
                     warehouse_code="WH1", state_code=None):
    """Create a client: schema, tables, seed data, first warehouse, grants, first Owner.

    Runs entirely on administrator credentials — the app role cannot create schemas,
    which is the point. Returns (tenant_row_id, schema, owner_username, temp_password).
    The Owner is created with must_change_password set: the operator knows the temporary
    password, so the first sign-in forces a private one.
    """
    from database.connection import SchemaPostgresqlDatabase
    from database.models import ALL_MODELS, Role, User, Warehouse
    from services import auth, bootstrap

    slug = valid_slug(slug)
    schema = SCHEMA_PREFIX + slug
    display_name = (display_name or "").strip()
    if not display_name:
        raise TenancyError("Give the client a display name.")
    owner_username = (owner_username or "").strip()
    if not owner_username or " " in owner_username:
        raise TenancyError("Owner username is required and cannot contain spaces.")

    cursor = admin_connection.cursor()
    ensure_registry(admin_connection)

    cursor.execute("SELECT 1 FROM public.tenant WHERE slug = %s OR schema_name = %s",
                   (slug, schema))
    if cursor.fetchone():
        raise TenancyError(f"A client with the slug '{slug}' already exists.")
    cursor.execute("SELECT 1 FROM public.user_directory WHERE username_lower = %s",
                   (owner_username.lower(),))
    if cursor.fetchone():
        raise TenancyError(
            f"The username '{owner_username}' is already taken on this platform.")

    password = owner_password or temp_password()
    problem = auth.password_problem(password)
    if problem:
        raise TenancyError(problem)

    # A schema with no tenant row is debris from a prior half-completed run: dropping it
    # lets a retry succeed instead of dying on 'schema already exists'.
    cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                   (schema,))
    if cursor.fetchone():
        log.warning("Dropping orphaned schema %s left by a previous failed run", schema)
        cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    params = admin_connection.get_dsn_parameters()
    schema_created = False
    tenant_id = None
    try:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
        schema_created = True
        log.info("Created schema %s", schema)

        # Build the tenant's tables on a dedicated admin connection bound to the new
        # schema, in one transaction so a mid-build failure commits nothing.
        tenant_db = SchemaPostgresqlDatabase(
            params.get("dbname", "postgres"),
            schema=schema,
            user=params.get("user"),
            password=admin_password,
            host=params.get("host"),
            port=int(params.get("port", 5432)),
            sslmode=config.PG_SSLMODE,
            **({"sslrootcert": config.PG_SSLROOTCERT} if config.PG_HAS_ROOTCERT else {}),
        )
        try:
            with tenant_db.bind_ctx(ALL_MODELS):
                tenant_db.connect()
                with tenant_db.atomic():
                    tenant_db.create_tables(ALL_MODELS)
                    bootstrap.seed_defaults()
                    warehouse = Warehouse.create(
                        code=(warehouse_code or "WH1").strip().upper(),
                        name=(warehouse_name or "Main Warehouse").strip(),
                        state_code=state_code,
                    )
                    owner = User.create(
                        username=owner_username,
                        password_hash=auth.hash_password(password),
                        full_name=owner_full_name or owner_username,
                        role=Role.OWNER,
                        warehouse=warehouse,
                        must_change_password=True,   # the operator knows this password
                    )
                    log.info("Seeded %s and created owner '%s' (id %s)",
                             schema, owner_username, owner.id)
        finally:
            if not tenant_db.is_closed():
                tenant_db.close()

        grant_app_access(cursor, schema)

        # The registry rows go last. The UNIQUE constraints on tenant.slug and
        # user_directory.username_lower are the authoritative guard against a concurrent
        # duplicate — the earlier SELECTs are only for a friendly message.
        cursor.execute(
            "INSERT INTO public.tenant (slug, display_name, schema_name, notes) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (slug, display_name, schema, notes))
        tenant_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO public.user_directory (username_lower, tenant_id) "
            "VALUES (%s, %s)",
            (owner_username.lower(), tenant_id))
    except Exception:
        # Compensate: leave neither an orphan schema nor a half-registered tenant. The
        # admin connection is autocommit, so these run immediately; deleting the tenant
        # row cascades to any user_directory entry.
        log.exception("Provisioning %s failed — rolling back", slug)
        try:
            if tenant_id is not None:
                cursor.execute("DELETE FROM public.tenant WHERE id = %s", (tenant_id,))
            if schema_created:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception:
            log.exception("Cleanup after failed provisioning also failed for %s", slug)
        raise

    return tenant_id, schema, owner_username, password


def drop_tenant(admin_connection, slug, *, confirm_slug):
    """Destroy a client and everything they own. Deliberately awkward to call."""
    slug = valid_slug(slug)
    if confirm_slug != slug:
        raise TenancyError("confirm_slug must repeat the slug exactly.")
    schema = SCHEMA_PREFIX + slug
    cursor = admin_connection.cursor()
    cursor.execute("DELETE FROM public.tenant WHERE slug = %s", (slug,))
    cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    log.warning("Dropped tenant %s (schema %s)", slug, schema)
