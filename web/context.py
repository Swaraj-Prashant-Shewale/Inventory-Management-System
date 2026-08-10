"""Per-request context: session → tenant → search_path → user.

The ordering is the whole game. A pooled connection remembers its search_path from the
previous request, which may have served a different client — so the tenant schema is
re-asserted on every request before any business query runs. The registry lookup that
decides the tenant is schema-qualified and therefore immune to whatever the path
currently is.
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from database.connection import configure_database, connect
from database.models import Role, User
from services import auth, tenancy
from web import security

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"

_jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(("html", "xml")),
    trim_blocks=True,
    lstrip_blocks=True,
)

_database_ready = False


def ensure_database():
    """Initialise the connection proxy once per process."""
    global _database_ready
    if not _database_ready:
        configure_database()
        _database_ready = True
    # Keep the per-thread connection open across requests: the Supabase handshake costs
    # ~1s cold, and ReconnectMixin re-establishes it transparently if it drops.
    connect(reuse=True)


@dataclass
class WebContext:
    request: Request
    user: object = None
    tenant: object = None
    admin: object = None
    session: dict = field(default_factory=dict)
    fresh_cookie: str = None

    @property
    def csrf(self) -> str:
        return (self.session or {}).get("csrf", "")

    def can(self, permission) -> bool:
        return auth.can(self.user, permission)


def resolve_user(request: Request) -> WebContext:
    """Best-effort context: user is None when not signed in. Never raises."""
    context = WebContext(request=request)
    ensure_database()

    payload = security.read_session(request.cookies.get(security.SESSION_COOKIE))
    if payload is None:
        return context

    tenant = tenancy.get_tenant(payload.get("t"))
    if tenant is None:
        return context

    try:
        tenancy.use_tenant(tenant)
        user = User.get_or_none(User.id == payload.get("u"))
    except Exception:
        log.exception("Failed to resolve the session user")
        return context

    if user is None or not user.is_active:
        return context

    context.user = user
    context.tenant = tenant
    context.session = payload
    context.fresh_cookie = security.refresh(payload)   # slide the idle window
    return context


def resolve_admin(request: Request) -> WebContext:
    """Context for the /admin panel. Multi-tenant (postgres) only."""
    context = WebContext(request=request)
    ensure_database()
    if not tenancy.is_multitenant():
        return context

    payload = security.read_session(
        request.cookies.get(security.ADMIN_COOKIE), expected_kind="admin")
    if payload is None:
        return context

    admin = tenancy.PlatformAdmin.get_or_none(
        tenancy.PlatformAdmin.id == payload.get("a"))
    if admin is None or not admin.is_active:
        return context

    context.admin = admin
    context.session = payload
    context.fresh_cookie = security.refresh(payload)
    return context


def render(context: WebContext, template, status_code=200, **values):
    """Render a template with the standard globals and carry the refreshed cookie."""
    from fastapi.responses import HTMLResponse

    template_obj = _jinja.get_template(template)
    html = template_obj.render(
        ctx=context,
        user=context.user,
        tenant=context.tenant,
        admin=context.admin,
        csrf=context.csrf,
        can=context.can,
        Role=Role,
        auth=auth,
        multitenant=tenancy.is_multitenant(),
        **values,
    )
    response = HTMLResponse(html, status_code=status_code)
    _carry_cookie(context, response)
    return response


def redirect(context: WebContext, url, status_code=303):
    response = RedirectResponse(url, status_code=status_code)
    _carry_cookie(context, response)
    return response


def _carry_cookie(context: WebContext, response):
    if context.fresh_cookie:
        name = security.ADMIN_COOKIE if context.admin else security.SESSION_COOKIE
        security.set_cookie(response, name, context.fresh_cookie)


def require_login(context: WebContext):
    """Redirect target for unauthenticated requests, or None when signed in."""
    if context.user is None:
        return RedirectResponse("/login", status_code=303)
    if context.user.must_change_password \
            and context.request.url.path not in ("/account/password", "/logout"):
        return redirect(context, "/account/password")
    return None


def forbidden(context: WebContext, action="do this"):
    return render(context, "error.html", status_code=403,
                  title="Not permitted",
                  message=f"Your role ({context.user.role_label if context.user else '—'}) "
                          f"is not allowed to {action}.")


def check_csrf(context: WebContext, submitted) -> bool:
    return security.csrf_matches(context.session, submitted)
