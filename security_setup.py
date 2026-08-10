"""Create the least-privilege database role the application runs as.

    .venv\\Scripts\\python.exe security_setup.py

Run this once, as an administrator, using the Supabase `postgres` credentials. It creates
a role that can read and write business data and nothing else, then prints the .env block
for the warehouse PCs. After this the superuser password never leaves your machine.

What the application role cannot do
-----------------------------------
* No DDL — cannot create, alter or drop a table, so it cannot destroy the schema.
* Not a superuser, cannot create roles or databases.
* Cannot delete or rewrite the audit trail. It may only null the user reference, which
  is what deleting a user account needs; the action, timestamp and summary are frozen.
* Cannot rewrite stock movement history. Quantities, balances and dates are immutable
  once written, so stock cannot be silently altered after the fact.
* Cannot touch anything outside the `public` schema.
"""
import getpass
import os
import secrets
import string
import sys

import psycopg2

import config

ROLE = "inventory_app"

# Columns that must stay writable so ON DELETE SET NULL can do its job. Everything else
# on these two tables is frozen once written.
AUDIT_NULLABLE_FKS = ["user_id"]
MOVEMENT_NULLABLE_FKS = ["lot_id", "user_id", "employee_id", "supplier_id",
                         "customer_id"]


def strong_password(length=32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def project_ref():
    """The Supabase tenant, taken from whichever role .env is configured with."""
    return config.PG_USER.split(".", 1)[1] if "." in config.PG_USER else None


def admin_credentials():
    """Ask for the superuser credentials rather than reading them from a file.

    `.env` holds the restricted role the app runs as. The administrator password
    deliberately lives nowhere on disk — it is typed in when it is needed, which is only
    here and for schema migrations.
    """
    ref = project_ref()
    default_user = f"postgres.{ref}" if ref else "postgres"

    user = os.environ.get("ADMIN_DB_USER") or ""
    if not user:
        entered = input(f"Administrator username [{default_user}]: ").strip()
        user = entered or default_user

    password = os.environ.get("ADMIN_DB_PASSWORD") or ""
    if not password:
        password = getpass.getpass(f"Password for {user} (not shown): ")
    if not password:
        raise SystemExit("No password given.")
    return user, password


def admin_connection():
    if not config.PG_CONFIGURED:
        raise SystemExit("DB_HOST in .env is not a real Supabase host.")

    user, password = admin_credentials()
    print(f"\nConnecting to {config.PG_HOST} as {user}…")
    params = dict(host=config.PG_HOST, port=config.PG_PORT, user=user,
                  password=password, dbname=config.PG_NAME,
                  sslmode=config.PG_SSLMODE, connect_timeout=20)
    if config.PG_HAS_ROOTCERT:
        params["sslrootcert"] = config.PG_SSLROOTCERT
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    return conn


def run(cur, sql, label, fatal=True):
    try:
        cur.execute(sql)
        print(f"  [ok] {label}")
        return True
    except Exception as exc:
        message = str(exc).strip().splitlines()[0]
        print(f"  [{'!!' if fatal else '--'}] {label} — {message}")
        if fatal:
            raise
        return False


def existing_columns(cur, table, wanted):
    cur.execute(
        "select column_name from information_schema.columns "
        "where table_schema = 'public' and table_name = %s", (table,))
    present = {row[0] for row in cur.fetchall()}
    return [column for column in wanted if column in present]


def apply(cur, password, role=ROLE):
    print("\n--- Role ---")
    cur.execute("select 1 from pg_roles where rolname = %s", (role,))
    if cur.fetchone():
        print(f"  role {role} already exists — resetting its password and privileges")
        cur.execute(f'ALTER ROLE {role} WITH LOGIN PASSWORD %s', (password,))
    else:
        cur.execute(f'CREATE ROLE {role} WITH LOGIN PASSWORD %s '
                    f'NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', (password,))
        print(f"  [ok] created {role}")

    # RLS is enabled on every table and the app is the only client; the real boundary is
    # the grants below, so let the app role through rather than writing 41 no-op policies.
    run(cur, f"ALTER ROLE {role} BYPASSRLS", "allow it past row level security")

    print("\n--- Reset any previous grants ---")
    run(cur, f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",
        "clear table grants", fatal=False)
    run(cur, f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}",
        "clear sequence grants", fatal=False)
    run(cur, f"REVOKE ALL ON SCHEMA public FROM {role}", "clear schema grants",
        fatal=False)

    print("\n--- Connect and read/write business data ---")
    run(cur, f'GRANT CONNECT ON DATABASE "{config.PG_NAME}" TO {role}',
        "connect to the database")
    run(cur, f"GRANT USAGE ON SCHEMA public TO {role}", "use the public schema")
    run(cur, f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
             f"TO {role}", "read and write existing tables")
    run(cur, f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        "use id sequences")

    print("\n--- Deny schema changes ---")
    # Without CREATE on the schema the role cannot add or replace objects; it already
    # owns nothing, so ALTER and DROP on existing tables are refused by ownership.
    run(cur, f"REVOKE CREATE ON SCHEMA public FROM {role}",
        "no creating objects (no DDL)")
    run(cur, f"REVOKE ALL ON SCHEMA information_schema FROM {role}",
        "no writing to information_schema", fatal=False)

    print("\n--- Freeze the audit trail ---")
    run(cur, f"REVOKE UPDATE, DELETE ON audit_log FROM {role}",
        "audit rows cannot be changed or deleted")
    columns = existing_columns(cur, "audit_log", AUDIT_NULLABLE_FKS)
    if columns:
        run(cur, f"GRANT UPDATE ({', '.join(columns)}) ON audit_log TO {role}",
            f"except clearing {', '.join(columns)} when a user is deleted")

    print("\n--- Freeze stock movement history ---")
    # DELETE must be revoked as well as UPDATE, or the append-only guarantee is a lie: a
    # compromised app could DELETE (or delete+reinsert to rewrite) the movement ledger.
    run(cur, f"REVOKE UPDATE, DELETE ON stock_movement FROM {role}",
        "quantities, balances and dates are immutable once written")
    columns = existing_columns(cur, "stock_movement", MOVEMENT_NULLABLE_FKS)
    if columns:
        run(cur, f"GRANT UPDATE ({', '.join(columns)}) ON stock_movement TO {role}",
            f"except clearing {', '.join(columns)} when a linked record is deleted")

    print("\n--- Future tables ---")
    # Only SELECT, INSERT by default. If UPDATE/DELETE were default-granted, any migration
    # that recreates audit_log or stock_movement would silently restore full write/delete
    # to the app role and lose the append-only protection. Tables needing UPDATE/DELETE
    # get it from the explicit ALL-TABLES grant above (applied when this script is re-run
    # after a migration), so this only affects the append-only ledgers.
    run(cur, f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
             f"GRANT SELECT, INSERT ON TABLES TO {role}",
        "tables added later are readable and insertable (not update/delete by default)")
    run(cur, f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
             f"GRANT USAGE, SELECT ON SEQUENCES TO {role}",
        "sequences added later are usable")


def verify(password, role=ROLE):
    """Reconnect as the new role and prove the limits actually hold."""
    print("\n--- Verifying the restrictions from the role's own session ---")
    # Supavisor addresses tenants as <role>.<project-ref>.
    ref = project_ref()
    login = f"{role}.{ref}" if ref else role

    conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT, user=login,
                            password=password, dbname=config.PG_NAME,
                            sslmode=config.PG_SSLMODE, connect_timeout=20)
    conn.autocommit = True
    cur = conn.cursor()

    results = []

    def expect(label, sql, should_fail):
        try:
            cur.execute(sql)
            ok = not should_fail
            results.append((label, ok, "allowed"))
        except Exception as exc:
            ok = should_fail
            results.append((label, ok, str(exc).strip().splitlines()[0][:60]))
        finally:
            try:
                conn.rollback()
            except Exception:
                pass

    cur.execute("select current_user")
    print(f"  connected as {cur.fetchone()[0]}")

    expect("read items", "select count(*) from item", should_fail=False)
    expect("read stock movements", "select count(*) from stock_movement",
           should_fail=False)
    expect("create a table", "create table hack_probe (id int)", should_fail=True)
    expect("drop a table", "drop table if exists item", should_fail=True)
    expect("delete audit rows", "delete from audit_log", should_fail=True)
    expect("rewrite an audit row", "update audit_log set action = 'tampered'",
           should_fail=True)
    expect("rewrite movement quantities", "update stock_movement set quantity = 0",
           should_fail=True)
    expect("delete movement rows", "delete from stock_movement", should_fail=True)
    expect("create a role", "create role sneaky login password 'x'", should_fail=True)

    for label, ok, detail in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {label}: {detail}")
    conn.close()
    return all(ok for _, ok, _ in results), login


def main():
    if config.DB_BACKEND != "postgres":
        print("Set DB_BACKEND=postgres in .env before running this.")
        return 1

    conn = admin_connection()
    cur = conn.cursor()

    cur.execute("select rolsuper, rolcreaterole from pg_roles where rolname = current_user")
    row = cur.fetchone()
    if not row or not row[1]:
        print("The connected role cannot create roles. Use the Supabase `postgres` "
              "credentials for this step.")
        return 1

    password = strong_password()
    apply(cur, password)
    conn.close()

    passed, login = verify(password)

    print("\n" + "=" * 72)
    if passed:
        print("All restrictions verified. Put this in the .env on every warehouse PC:\n")
    else:
        print("SOME RESTRICTIONS DID NOT HOLD — review the output above.\n")
    print(f"DB_USER={login}")
    print(f"DB_PASSWORD={password}")
    print("\nKeep the postgres superuser credentials off the warehouse PCs entirely.")
    print("Store them somewhere safe — they are needed for schema changes and for")
    print("re-running this script.")
    print("=" * 72)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
