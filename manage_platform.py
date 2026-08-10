"""Platform operations: create administrators and provision client companies.

    .venv\\Scripts\\python.exe manage_platform.py create-admin
    .venv\\Scripts\\python.exe manage_platform.py create-tenant acme "Acme Traders"
    .venv\\Scripts\\python.exe manage_platform.py list
    .venv\\Scripts\\python.exe manage_platform.py drop-tenant acme

Every command asks for the database administrator password at the keyboard (or reads
ADMIN_DB_USER / ADMIN_DB_PASSWORD from the environment). The application role cannot do
any of this — schema creation and tenant management are deliberately administrator-only.
"""
import argparse
import getpass
import os
import sys

import psycopg2

import config


def admin_connection():
    if config.DB_BACKEND != "postgres":
        raise SystemExit("Multi-tenant operations need DB_BACKEND=postgres in .env.")
    if not config.PG_CONFIGURED:
        raise SystemExit("DB_HOST in .env is not a real Supabase host.")

    ref = config.PG_USER.split(".", 1)[1] if "." in config.PG_USER else None
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

    params = dict(host=config.PG_HOST, port=config.PG_PORT, user=user,
                  password=password, dbname=config.PG_NAME,
                  sslmode=config.PG_SSLMODE, connect_timeout=20)
    if config.PG_HAS_ROOTCERT:
        params["sslrootcert"] = config.PG_SSLROOTCERT
    connection = psycopg2.connect(**params)
    connection.autocommit = True
    return connection, password


def cmd_create_admin(args):
    from services import auth, tenancy

    connection, _ = admin_connection()
    tenancy.ensure_registry(connection)
    cursor = connection.cursor()

    username = args.username or input("Admin panel username: ").strip()
    if not username or " " in username:
        raise SystemExit("Username is required and cannot contain spaces.")
    full_name = args.full_name or input("Full name: ").strip() or username

    password = getpass.getpass("Admin panel password (min 8 chars, not shown): ")
    problem = auth.password_problem(password)
    if problem:
        raise SystemExit(problem)
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")

    cursor.execute("SELECT 1 FROM public.platform_admin WHERE username = %s",
                   (username,))
    if cursor.fetchone():
        cursor.execute(
            "UPDATE public.platform_admin SET password_hash = %s, is_active = TRUE, "
            "failed_attempts = 0, locked_until = NULL WHERE username = %s",
            (auth.hash_password(password), username))
        print(f"Updated existing platform admin '{username}'.")
    else:
        cursor.execute(
            "INSERT INTO public.platform_admin (username, password_hash, full_name) "
            "VALUES (%s, %s, %s)",
            (username, auth.hash_password(password), full_name))
        print(f"Created platform admin '{username}'.")
    print("Sign in at /admin on the website.")


def cmd_create_tenant(args):
    from services import tenancy

    connection, admin_password = admin_connection()

    owner_username = args.owner or input("Owner username for this client: ").strip()
    owner_name = args.owner_name or input("Owner full name: ").strip() or owner_username
    state = args.state or input("Warehouse GST state code (e.g. 27, blank to skip): "
                                ).strip() or None

    tenant_id, schema, username, password = tenancy.provision_tenant(
        connection, admin_password,
        slug=args.slug, display_name=args.name,
        owner_username=owner_username, owner_full_name=owner_name,
        state_code=state, notes=args.notes,
    )

    print("\n" + "=" * 64)
    print(f"Client created: {args.name}")
    print(f"  slug    : {args.slug}")
    print(f"  schema  : {schema}")
    print(f"  registry id: {tenant_id}")
    print("\nHand these to the client's owner:")
    print(f"  username : {username}")
    print(f"  password : {password}   (they must change it at first sign-in)")
    print("=" * 64)


def cmd_list(args):
    connection, _ = admin_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT t.slug, t.display_name, t.schema_name, t.is_active, t.created_at,
               (SELECT COUNT(*) FROM public.user_directory d WHERE d.tenant_id = t.id)
        FROM public.tenant t ORDER BY t.slug""")
    rows = cursor.fetchall()
    if not rows:
        print("No clients provisioned yet.")
        return
    print(f"{'slug':16} {'name':28} {'schema':20} {'active':6} {'users':5} created")
    for slug, name, schema, active, created, users in rows:
        print(f"{slug:16} {name[:27]:28} {schema:20} {str(active):6} {users:5} "
              f"{created:%d %b %Y}")


def cmd_drop_tenant(args):
    from services import tenancy

    print(f"This permanently destroys client '{args.slug}' and ALL their data.")
    if input(f"Type the slug '{args.slug}' to confirm: ").strip() != args.slug:
        raise SystemExit("Not confirmed — nothing done.")
    connection, _ = admin_connection()
    tenancy.drop_tenant(connection, args.slug, confirm_slug=args.slug)
    print(f"Client '{args.slug}' dropped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", help="create or reset a platform admin login")
    p.add_argument("--username")
    p.add_argument("--full-name", dest="full_name")
    p.set_defaults(fn=cmd_create_admin)

    p = sub.add_parser("create-tenant", help="provision a new client company")
    p.add_argument("slug", help="short id, e.g. acme_traders")
    p.add_argument("name", help="display name, e.g. \"Acme Traders Pvt Ltd\"")
    p.add_argument("--owner", help="owner username")
    p.add_argument("--owner-name", dest="owner_name")
    p.add_argument("--state", help="warehouse GST state code, e.g. 27")
    p.add_argument("--notes")
    p.set_defaults(fn=cmd_create_tenant)

    p = sub.add_parser("list", help="list provisioned clients")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("drop-tenant", help="destroy a client and all their data")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_drop_tenant)

    args = parser.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
