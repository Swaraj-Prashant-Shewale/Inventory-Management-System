"""Sign-in, sign-out and the forced password change."""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from services import auth, tenancy
from web import security
from web.context import (
    check_csrf,
    redirect,
    render,
    resolve_user,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _login_page(context, status_code=200, **values):
    """Render the login page and (re)issue its anti-CSRF token cookie."""
    token = security.issue_login_csrf()
    response = render(context, "login.html", status_code=status_code,
                      login_csrf=token, **values)
    security.set_cookie(response, security.LOGIN_CSRF_COOKIE, token,
                        max_age=security.LOGIN_CSRF_MAX_AGE)
    return response


@router.get("/login")
def login_page(request: Request):
    context = resolve_user(request)
    if context.user is not None:
        return redirect(context, "/")
    return _login_page(context)


@router.post("/login")
def login_submit(request: Request, username: str = Form(""),
                 password: str = Form(""), csrf_token: str = Form("")):
    context = resolve_user(request)

    if not security.login_csrf_valid(
            request.cookies.get(security.LOGIN_CSRF_COOKIE), csrf_token):
        # Blocks login CSRF: a cross-site forced login carries no matching token cookie.
        return _login_page(context, status_code=400, username=username,
                           error="Your session expired — please try again.")

    tenant = tenancy.find_tenant_for_username(username)
    if tenant is None:
        # Run a dummy verification so a missing username costs the same time as a wrong
        # password — otherwise response latency would reveal which usernames exist.
        auth.dummy_verify()
        # Same wording as a wrong password, so the form can't be used to
        # discover which usernames exist on the platform.
        return _login_page(context, status_code=401,
                           error="Incorrect username or password.", username=username)

    try:
        tenancy.use_tenant(tenant)
        user = auth.authenticate(username, password)
    except auth.AuthError as exc:
        return _login_page(context, status_code=401, error=str(exc), username=username)

    response = RedirectResponse("/", status_code=303)
    security.set_cookie(response, security.SESSION_COOKIE,
                        security.issue_session(user.id, getattr(tenant, "id", 0)))
    security.clear_cookie(response, security.LOGIN_CSRF_COOKIE)
    return response


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    context = resolve_user(request)
    if context.user is not None:
        auth.record_audit(context.user, "LOGOUT",
                          summary=f"{context.user.full_name} signed out")
    response = RedirectResponse("/login", status_code=303)
    security.clear_cookie(response, security.SESSION_COOKIE)
    return response


@router.get("/account/password")
def password_page(request: Request):
    context = resolve_user(request)
    if context.user is None:
        return RedirectResponse("/login", status_code=303)
    return render(context, "change_password.html",
                  forced=context.user.must_change_password)


@router.post("/account/password")
def password_submit(request: Request, current: str = Form(""),
                    new: str = Form(""), confirm: str = Form(""),
                    csrf_token: str = Form("")):
    context = resolve_user(request)
    if context.user is None:
        return RedirectResponse("/login", status_code=303)
    if not check_csrf(context, csrf_token):
        return render(context, "change_password.html", status_code=400,
                      forced=context.user.must_change_password,
                      error="The form expired — try again.")

    if new != confirm:
        return render(context, "change_password.html", status_code=400,
                      forced=context.user.must_change_password,
                      error="The two passwords do not match.")
    try:
        auth.change_password(context.user, new, current_password=current)
    except auth.AuthError as exc:
        return render(context, "change_password.html", status_code=400,
                      forced=context.user.must_change_password, error=str(exc))

    return redirect(context, "/")
