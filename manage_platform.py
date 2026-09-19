"""Platform operations: create administrators and provision client companies.

    .venv\\Scripts\\python.exe manage_platform.py create-admin
    .venv\\Scripts\\python.exe manage_platform.py admin-2fa --username adnan
    .venv\\Scripts\\python.exe manage_platform.py setup-web-provisioner
    .venv\\Scripts\\python.exe manage_platform.py create-tenant acme "Acme Traders"
    .venv\\Scripts\\python.exe manage_platform.py list
    .venv\\Scripts\\python.exe manage_platform.py reset-owner acme
    .venv\\Scripts\\python.exe manage_platform.py suspend-tenant acme
    .venv\\Scripts\\python.exe manage_platform.py reactivate-tenant acme
    .venv\\Scripts\\python.exe manage_platform.py drop-tenant acme

Every command asks for the database administrator password at the keyboard (or reads
ADMIN_DB_USER / ADMIN_DB_PASSWORD from the environment). The application role cannot do
any of this — schema creation and tenant management are deliberately administrator-only.
"""
import argparse
import getpass
import os
import secrets
import sys

import psycopg2
from psycopg2 import sql

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
        entered = input(
            "Supabase database administrator username\n"
            f"  (press Enter to use {default_user}): "
        ).strip()
        user = entered or default_user
    if ".pooler.supabase.com" in config.PG_HOST and "." not in user:
        raise SystemExit(
            "The Supabase pooler username must include the project reference, for "
            f"example '{default_user}'. This is the database login, not the website "
            "administrator username. Run the command again and press Enter to use "
            "the displayed default."
        )
    password = os.environ.get("ADMIN_DB_PASSWORD") or ""
    if not password:
        password = getpass.getpass(
            f"Supabase database password for {user} (not shown): "
        )
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


def _print_qr(uri):
    """Draw a scannable QR in the terminal if the optional 'qrcode' package is present."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print("  (Install the optional 'qrcode' package to show a scannable box here,")
        print("   or paste the Link below into any authenticator that accepts an")
        print("   otpauth:// URL.)")


def cmd_admin_2fa(args):
    from services import tenancy, totp

    connection, _ = admin_connection()
    tenancy.ensure_registry(connection)   # makes sure the totp_secret column exists
    cursor = connection.cursor()

    username = args.username or input("Admin username: ").strip()
    cursor.execute("SELECT 1 FROM public.platform_admin WHERE username = %s", (username,))
    if not cursor.fetchone():
        raise SystemExit(f"No platform admin named '{username}'. "
                         f"Create it first with create-admin.")

    if args.disable:
        cursor.execute("UPDATE public.platform_admin SET totp_secret = NULL "
                       "WHERE username = %s", (username,))
        print(f"Two-factor authentication DISABLED for '{username}'.")
        return

    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, account=username)
    print("\nScan this in Google Authenticator, Authy, Microsoft Authenticator or "
          "1Password:\n")
    _print_qr(uri)
    print(f"\n  Or enter this secret by hand : {secret}")
    print(f"  Link                         : {uri}\n")

    # Confirm the app is set up BEFORE saving, so a mis-scan cannot lock you out.
    code = input("Enter the 6-digit code your app shows now, to confirm: ").strip()
    if not totp.verify(secret, code):
        raise SystemExit("That code did not match — nothing was saved. Run this again.")

    cursor.execute("UPDATE public.platform_admin SET totp_secret = %s "
                   "WHERE username = %s", (secret, username))
    print(f"\nTwo-factor authentication ENABLED for '{username}'.")
    print("The /admin sign-in now also asks for the 6-digit code.")
    print("Lost the phone? Re-run:  manage_platform.py admin-2fa --username "
          f"{username} --disable   (needs the master password).")


def cmd_setup_web_provisioner(args):
    """Create or rotate the limited database login used by the hosted admin form."""
    from services import tenancy

    connection, _ = admin_connection()
    tenancy.ensure_registry(connection)
    cursor = connection.cursor()
    role = "inventory_provisioner"

    cursor.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    )
    existing = cursor.fetchone()
    if existing and any(existing):
        raise SystemExit(
            f"Refusing to reuse elevated role '{role}'. Remove its elevated "
            "attributes or choose a clean database before continuing."
        )

    password = secrets.token_urlsafe(32)
    if existing:
        cursor.execute(
            # Elevated attributes were verified false above. Supabase's dashboard
            # postgres role can rotate an ordinary password, but only a true
            # PostgreSQL superuser may issue NOSUPERUSER in ALTER ROLE.
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s")
            .format(sql.Identifier(role)),
            (password,),
        )
    else:
        cursor.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION PASSWORD %s")
            .format(sql.Identifier(role)),
            (password,),
        )

    cursor.execute(
        sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}")
        .format(sql.Identifier(config.PG_NAME), sql.Identifier(role))
    )
    cursor.execute(
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}")
        .format(sql.Identifier(role))
    )
    cursor.execute(
        sql.SQL("GRANT SELECT, INSERT, DELETE ON public.tenant, "
                "public.user_directory TO {}")
        .format(sql.Identifier(role))
    )
    cursor.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
        .format(sql.Identifier(role))
    )
    # Table grants and RLS policies are separate gates. Keep policies limited to the
    # two registry tables needed during provisioning; SQL grants still deny UPDATE.
    for table in ("tenant", "user_directory"):
        cursor.execute(
            sql.SQL("DROP POLICY IF EXISTS inventory_provisioner_access "
                    "ON public.{}")
            .format(sql.Identifier(table))
        )
        cursor.execute(
            sql.SQL("CREATE POLICY inventory_provisioner_access ON public.{} "
                    "FOR ALL TO {} USING (true) WITH CHECK (true)")
            .format(sql.Identifier(table), sql.Identifier(role))
        )
    cursor.execute(
        sql.SQL("REVOKE ALL ON public.platform_admin FROM {}")
        .format(sql.Identifier(role))
    )

    ref = config.PG_USER.split(".", 1)[1] if "." in config.PG_USER else ""
    pooler_user = f"{role}.{ref}" if ref else role
    print("\nLimited web provisioner is ready.")
    print("Add these as Sensitive Production variables in Vercel, then redeploy:")
    print(f"  PROVISION_DB_USER={pooler_user}")
    print(f"  PROVISION_DB_PASSWORD={password}")
    print("\nThe password is shown once. Do not add the Supabase postgres password.")


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


def cmd_reset_owner(args):
    from services import tenancy

    connection, admin_password = admin_connection()
    username, password = tenancy.reset_owner_password(
        connection, admin_password, args.slug, username=args.user)
    print("\n" + "=" * 64)
    print(f"Password reset for client '{args.slug}'.")
    print("\nHand these to the person:")
    print(f"  username : {username}")
    print(f"  password : {password}   (they must change it at first sign-in)")
    print("=" * 64)


def cmd_suspend_tenant(args):
    from services import tenancy

    connection, _ = admin_connection()
    name = tenancy.set_tenant_active(connection, args.slug, False)
    print(f"Client '{args.slug}' ({name}) is SUSPENDED.")
    print("Their logins are blocked immediately, but all their data is kept.")
    print(f"Undo with:  manage_platform.py reactivate-tenant {args.slug}")


def cmd_reactivate_tenant(args):
    from services import tenancy

    connection, _ = admin_connection()
    name = tenancy.set_tenant_active(connection, args.slug, True)
    print(f"Client '{args.slug}' ({name}) is active again — their logins work.")


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

    p = sub.add_parser("admin-2fa",
                       help="enable (or --disable) two-factor auth for a platform admin")
    p.add_argument("--username")
    p.add_argument("--disable", action="store_true",
                   help="turn 2FA off instead of on")
    p.set_defaults(fn=cmd_admin_2fa)

    p = sub.add_parser(
        "setup-web-provisioner",
        help="create or rotate the limited database role used by the web admin form",
    )
    p.set_defaults(fn=cmd_setup_web_provisioner)

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

    p = sub.add_parser("reset-owner",
                       help="reset a client owner's password to a temporary one")
    p.add_argument("slug")
    p.add_argument("--user", help="a specific username (default: the client's owner)")
    p.set_defaults(fn=cmd_reset_owner)

    p = sub.add_parser("suspend-tenant",
                       help="block a client's logins without deleting their data")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_suspend_tenant)

    p = sub.add_parser("reactivate-tenant", help="re-enable a suspended client")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_reactivate_tenant)

    p = sub.add_parser("drop-tenant", help="destroy a client and all their data")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_drop_tenant)

    args = parser.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
