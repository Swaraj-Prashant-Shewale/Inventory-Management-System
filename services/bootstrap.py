"""First-run setup: create the schema and seed the minimum data the app needs to open."""
import logging

import config
from database.connection import configure_database, connect, db, is_postgres
from database.models import (
    ALL_MODELS,
    CompanySettings,
    NumberSequence,
    PriceList,
    Role,
    Uom,
    User,
    Warehouse,
)
from services import auth, numbering

log = logging.getLogger(__name__)

DEFAULT_UOMS = [
    ("EA", "Each", False),
    ("BOX", "Box", False),
    ("CTN", "Carton", False),
    ("PKT", "Packet", False),
    ("KG", "Kilogram", True),
    ("G", "Gram", True),
    ("L", "Litre", True),
    ("ML", "Millilitre", True),
    ("M", "Metre", True),
    ("PAL", "Pallet", False),
]


def schema_exists() -> bool:
    """Cheap single-query check for whether the schema has already been built.

    Scoped to the configured Postgres schema on purpose: peewee's table_exists() looks
    across the whole search path, so pointing at an empty schema while `public` is
    populated would wrongly report the tables as already there.
    """
    table = CompanySettings._meta.table_name
    try:
        if is_postgres():
            row = db.execute_sql(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s LIMIT 1",
                (config.PG_SCHEMA, table),
            ).fetchone()
            return row is not None
        return CompanySettings.table_exists()
    except Exception:
        return False


def initialize(create_schema: bool = True):
    """Connect, build the schema if it is missing, and seed baseline records.

    `create_tables` is deliberately not run on every startup: over a remote database it
    issues a few hundred round trips checking 41 tables and 176 indexes, which added
    roughly half a minute to every launch.
    """
    configure_database()
    connect()

    fresh = False
    if create_schema and not schema_exists():
        log.info("Schema not found — creating tables")
        db.create_tables(ALL_MODELS, safe=True)
        fresh = True

    seed_defaults(force=fresh)
    return db


def seed_defaults(force: bool = False):
    """Insert the rows the app assumes exist. Idempotent and cheap to re-run.

    Each check reads the whole small lookup table in one query rather than issuing a
    get_or_create per row, so a warm start costs a handful of round trips, not dozens.
    """
    existing_sequences = {row.doc_type for row in NumberSequence.select(
        NumberSequence.doc_type)}
    missing_sequences = [
        (doc_type, spec) for doc_type, spec in numbering.DEFAULT_SEQUENCES.items()
        if doc_type not in existing_sequences
    ]
    if missing_sequences:
        NumberSequence.insert_many([
            {"doc_type": doc_type, "prefix": prefix, "next_number": 1, "padding": 4,
             "include_fy": include_fy}
            for doc_type, (prefix, include_fy) in missing_sequences
        ]).execute()

    existing_uoms = {row.code for row in Uom.select(Uom.code)}
    missing_uoms = [u for u in DEFAULT_UOMS if u[0] not in existing_uoms]
    if missing_uoms:
        Uom.insert_many([
            {"code": code, "name": name, "allow_decimal": allow_decimal}
            for code, name, allow_decimal in missing_uoms
        ]).execute()

    if force or not CompanySettings.select().exists():
        if not CompanySettings.select().exists():
            CompanySettings.create(legal_name="My Company")

    if force or not PriceList.select().exists():
        if not PriceList.select().exists():
            PriceList.create(name="Retail", is_default=True,
                             description="Standard list price")
            PriceList.create(name="Wholesale", discount_percent=10,
                             description="Bulk buyers — 10% off list")


def needs_setup() -> bool:
    """True when nobody can log in yet, i.e. this is a fresh installation."""
    return not User.select().where(User.is_active == True).exists()  # noqa: E712


def create_first_owner(username: str, password: str, full_name: str,
                       warehouse_name: str = "Main Warehouse",
                       warehouse_code: str = "WH1", state_code: str = None) -> User:
    """Create the initial Owner account and the first warehouse.

    Refuses to run once any user exists, so it can't be used to mint an extra owner.
    """
    if User.select().exists():
        raise auth.AuthError("Setup has already been completed for this database.")

    problem = auth.password_problem(password)
    if problem:
        raise auth.AuthError(problem)

    with db.atomic():
        warehouse, _ = Warehouse.get_or_create(
            code=warehouse_code,
            defaults={"name": warehouse_name, "state_code": state_code, "is_active": True},
        )
        user = User.create(
            username=username.strip(),
            password_hash=auth.hash_password(password),
            full_name=full_name.strip() or username.strip(),
            role=Role.OWNER,
            warehouse=warehouse,
            is_active=True,
        )
    auth.record_audit(user, "SETUP", summary=f"Initial owner account '{username}' created")
    return user
