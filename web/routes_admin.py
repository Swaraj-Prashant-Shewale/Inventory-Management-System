"""Platform admin panel: sign in, see clients, provision new ones.

Provisioning creates schemas, which the app role deliberately cannot do — so the panel
only offers it when administrator credentials are provided to the server via
ADMIN_DB_USER / ADMIN_DB_PASSWORD environment variables. Without them it lists clients
and points the operator at manage_platform.py.
"""
import datetime
import logging
import os

import psycopg2
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

import config
from services import auth, tenancy
from services import totp as totp_svc
from web import ratelimit, security
from web.context import check_csrf, redirect, render, resolve_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def _server_admin_credentials():
    user = os.environ.get("PROVISION_DB_USER", "").strip()
    password = os.environ.get("PROVISION_DB_PASSWORD", "").strip()
    if user and password:
        return user, password

    # Backward compatibility for private, persistent deployments.
    user = os.environ.get("ADMIN_DB_USER", "").strip()
    password = os.environ.get("ADMIN_DB_PASSWORD", "").strip()
    if user and password:
        return user, password
    return None, None


def _using_limited_provisioner():
    return bool(
        os.environ.get("PROVISION_DB_USER", "").strip()
        and os.environ.get("PROVISION_DB_PASSWORD", "").strip()
    )


def _admin_connection():
    user, password = _server_admin_credentials()
    if not user:
        return None, None
    params = dict(host=config.PG_HOST, port=config.PG_PORT, user=user,
                  password=password, dbname=config.PG_NAME,
                  sslmode=config.PG_SSLMODE, connect_timeout=20)
    if config.PG_HAS_ROOTCERT:
        params["sslrootcert"] = config.PG_SSLROOTCERT
    connection = psycopg2.connect(**params)
    connection.autocommit = True
    return connection, password


def _admin_login_page(context, status_code=200, **values):
    token = security.issue_login_csrf()
    response = render(context, "admin/login.html", status_code=status_code,
                      login_csrf=token, **values)
    security.set_cookie(response, security.LOGIN_CSRF_COOKIE, token,
                        max_age=security.LOGIN_CSRF_MAX_AGE)
    return response


@router.get("/login")
def admin_login_page(request: Request):
    context = resolve_admin(request)
    if not tenancy.is_multitenant():
        return render(context, "admin/unavailable.html", status_code=404)
    if context.admin is not None:
        return redirect(context, "/admin")
    return _admin_login_page(context)


@router.post("/login")
def admin_login_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), totp: str = Form(""),
                       csrf_token: str = Form("")):
    context = resolve_admin(request)
    if not tenancy.is_multitenant():
        return render(context, "admin/unavailable.html", status_code=404)

    def fail(message, status_code=401):
        return _admin_login_page(context, status_code=status_code, error=message,
                                 username=username)

    if not security.login_csrf_valid(
            request.cookies.get(security.LOGIN_CSRF_COOKIE), csrf_token):
        return fail("Your session expired — please try again.", status_code=400)

    # Throttle per IP before the PBKDF2 verification (see web/ratelimit.py).
    ip = ratelimit.client_ip(request)
    wait = ratelimit.ADMIN_LOGIN_LIMITER.retry_after(ip)
    if wait:
        response = fail(
            f"Too many sign-in attempts. Please wait {wait} seconds and try again.",
            status_code=429)
        response.headers["Retry-After"] = str(wait)
        return response

    admin = tenancy.PlatformAdmin.get_or_none(
        tenancy.PlatformAdmin.username == (username or "").strip())
    now = datetime.datetime.now()

    if admin is None:
        auth.dummy_verify(password)   # equalize timing with the real path
        return fail("Incorrect username or password.")
    if admin.locked_until and admin.locked_until > now:
        minutes = int((admin.locked_until - now).total_seconds() // 60) + 1
        return fail(f"Locked after repeated failures — try again in {minutes} min.")
    if not admin.is_active:
        return fail("This administrator account is disabled.")

    # The app role may only write these three columns of platform_admin — see the grant
    # in tenancy.ensure_registry. A bare .save() would try to rewrite every column,
    # including password_hash, and be refused. Always restrict the write.
    tracking = [tenancy.PlatformAdmin.failed_attempts,
                tenancy.PlatformAdmin.locked_until,
                tenancy.PlatformAdmin.last_login_at]

    def register_failure(message):
        admin.failed_attempts = (admin.failed_attempts or 0) + 1
        if admin.failed_attempts >= LOCKOUT_THRESHOLD:
            admin.locked_until = now + datetime.timedelta(minutes=LOCKOUT_MINUTES)
            admin.failed_attempts = 0
        admin.save(only=tracking)
        return fail(message)

    if not auth.verify_password(password, admin.password_hash):
        return register_failure("Incorrect username or password.")

    # Second factor. A wrong code after a correct password still counts toward the lockout,
    # so an attacker who somehow has the password cannot brute-force the 6-digit code (5
    # tries then locked). The code field is ignored for admins without 2FA enabled.
    if admin.totp_secret and not totp_svc.verify(admin.totp_secret, totp):
        return register_failure("Incorrect authentication code — check your app and retry.")

    ratelimit.ADMIN_LOGIN_LIMITER.reset(ip)
    admin.failed_attempts = 0
    admin.locked_until = None
    admin.last_login_at = now
    admin.save(only=tracking)

    response = RedirectResponse("/admin", status_code=303)
    security.set_cookie(response, security.ADMIN_COOKIE,
                        security.issue_admin_session(admin.id))
    security.clear_cookie(response, security.LOGIN_CSRF_COOKIE)
    return response


@router.get("/logout")
@router.post("/logout")
def admin_logout(request: Request):
    response = RedirectResponse("/admin/login", status_code=303)
    security.clear_cookie(response, security.ADMIN_COOKIE)
    return response


@router.get("")
@router.get("/")
def admin_home(request: Request):
    context = resolve_admin(request)
    if not tenancy.is_multitenant():
        return render(context, "admin/unavailable.html", status_code=404)
    if context.admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    tenants = list(tenancy.Tenant.select().order_by(tenancy.Tenant.slug))
    directory_counts = {}
    for entry in tenancy.UserDirectory.select():
        directory_counts[entry.tenant_id] = directory_counts.get(entry.tenant_id, 0) + 1

    can_provision = _server_admin_credentials()[0] is not None
    return render(context, "admin/tenants.html", tenants=tenants,
                  user_counts=directory_counts, can_provision=can_provision,
                  created=request.query_params.get("created"))


@router.post("/tenants")
def admin_create_tenant(request: Request, slug: str = Form(""),
                        display_name: str = Form(""), owner_username: str = Form(""),
                        owner_name: str = Form(""), state_code: str = Form(""),
                        csrf_token: str = Form("")):
    context = resolve_admin(request)
    if context.admin is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not check_csrf(context, csrf_token):
        return _tenants_error(context, "The form expired — try again.")

    connection, admin_password = _admin_connection()
    if connection is None:
        return _tenants_error(
            context,
            "Provisioning needs PROVISION_DB_USER and PROVISION_DB_PASSWORD set in "
            "Vercel. Run manage_platform.py setup-web-provisioner locally first.")

    try:
        tenant_id, schema, username, password = tenancy.provision_tenant(
            connection, admin_password,
            slug=slug, display_name=display_name,
            owner_username=owner_username, owner_full_name=owner_name,
            state_code=(state_code or "").strip() or None,
            ensure_registry_first=not _using_limited_provisioner(),
        )
    except tenancy.TenancyError as exc:
        return _tenants_error(context, str(exc))
    except Exception:
        # Log the detail; show the operator a generic message rather than raw driver/SQL
        # error text (schema names, constraints), matching the app-wide no-leak policy.
        log.exception("Tenant provisioning failed")
        return _tenants_error(context,
                              "Provisioning failed — check the server logs for details.")
    finally:
        connection.close()

    log.info("Admin '%s' provisioned tenant '%s'", context.admin.username, slug)
    return render(context, "admin/created.html",
                  slug=slug, display_name=display_name, schema=schema,
                  owner_username=username, owner_password=password)


@router.post("/tenants/{slug}/reset-owner")
def admin_reset_owner(request: Request, slug: str,
                      csrf_token: str = Form("")):
    context = resolve_admin(request)
    if context.admin is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not check_csrf(context, csrf_token):
        return _tenants_error(context, "The form expired — try again.")

    connection, provision_password = _admin_connection()
    if connection is None:
        return _tenants_error(
            context,
            "Password reset needs PROVISION_DB_USER and PROVISION_DB_PASSWORD "
            "set in Vercel."
        )

    try:
        username, password = tenancy.reset_owner_password(
            connection, provision_password, slug
        )
    except tenancy.TenancyError as exc:
        return _tenants_error(context, str(exc))
    except Exception:
        log.exception("Owner password reset failed for tenant '%s'", slug)
        return _tenants_error(
            context, "Password reset failed — check the server logs for details."
        )
    finally:
        connection.close()

    log.warning(
        "Admin '%s' reset the owner password for tenant '%s'",
        context.admin.username, slug,
    )
    return render(
        context, "admin/owner_reset.html",
        slug=slug, owner_username=username, owner_password=password,
    )


def _tenants_error(context, message):
    tenants = list(tenancy.Tenant.select().order_by(tenancy.Tenant.slug))
    return render(context, "admin/tenants.html", status_code=400,
                  tenants=tenants, user_counts={}, error=message,
                  can_provision=_server_admin_credentials()[0] is not None)
