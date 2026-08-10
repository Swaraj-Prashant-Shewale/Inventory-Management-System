"""Authentication, password hashing and the role permission matrix.

Hashing uses PBKDF2-HMAC-SHA256 from the standard library — no compiled dependency, which
keeps the PyInstaller build simple and portable.
"""
import base64
import datetime
import hashlib
import hmac
import logging
import os
import secrets

import config
from database.models import AuditLog, Role, User

log = logging.getLogger(__name__)

HASH_SCHEME = "pbkdf2_sha256"
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 10


class AuthError(Exception):
    """Login refused. The message is safe to show the user."""


# --- Permissions --------------------------------------------------------------------
# One capability per gated action. Screens and buttons ask `can(user, PERM)`.

PERM_VIEW_INVENTORY = "view_inventory"
PERM_VIEW_ANALYTICS = "view_analytics"
PERM_VIEW_COST = "view_cost"
PERM_MANAGE_ITEMS = "manage_items"
PERM_MANAGE_PARTNERS = "manage_partners"
PERM_MANAGE_PRICING = "manage_pricing"
PERM_CREATE_PURCHASE = "create_purchase"
PERM_RECEIVE_GOODS = "receive_goods"
PERM_CREATE_SALES = "create_sales"
PERM_FULFILL_GOODS = "fulfill_goods"
PERM_RECORD_PAYMENT = "record_payment"
PERM_ADJUST_STOCK = "adjust_stock"
PERM_APPROVE_ADJUSTMENT = "approve_adjustment"
PERM_TRANSFER_STOCK = "transfer_stock"
PERM_CYCLE_COUNT = "cycle_count"
PERM_MANAGE_TASKS = "manage_tasks"
PERM_MANAGE_EMPLOYEES = "manage_employees"
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_SETTINGS = "manage_settings"
PERM_DELETE_RECORDS = "delete_records"

_CLERK = {
    PERM_VIEW_INVENTORY,
    PERM_CREATE_PURCHASE,
    PERM_RECEIVE_GOODS,
    PERM_CREATE_SALES,
    PERM_FULFILL_GOODS,
    PERM_ADJUST_STOCK,
    PERM_CYCLE_COUNT,
    PERM_TRANSFER_STOCK,
}

_MANAGER = _CLERK | {
    PERM_VIEW_ANALYTICS,
    PERM_VIEW_COST,
    PERM_MANAGE_ITEMS,
    PERM_MANAGE_PARTNERS,
    PERM_MANAGE_PRICING,
    PERM_RECORD_PAYMENT,
    PERM_APPROVE_ADJUSTMENT,
    PERM_MANAGE_TASKS,
    PERM_MANAGE_EMPLOYEES,
}

_OWNER = _MANAGER | {
    PERM_MANAGE_USERS,
    PERM_MANAGE_SETTINGS,
    PERM_DELETE_RECORDS,
}

ROLE_PERMISSIONS = {
    Role.CLERK: _CLERK,
    Role.MANAGER: _MANAGER,
    Role.OWNER: _OWNER,
}


def can(user, permission) -> bool:
    """True if `user` holds `permission`. A missing user has no rights at all."""
    if user is None or not user.is_active:
        return False
    return permission in ROLE_PERMISSIONS.get(user.role, set())


class NotAuthorised(PermissionError):
    """A caller lacked the capability. The message is safe to show the user."""


def require(user, permission, action=None):
    """Refuse unless `user` holds `permission`.

    Called at the top of every operation that writes business data, so authorisation
    does not depend on a button having been disabled. A missing user is refused rather
    than waved through — an unattributed write is exactly what should not happen.
    """
    if user is None:
        raise NotAuthorised(
            f"You must be signed in to {action or 'do this'}."
        )
    if not can(user, permission):
        raise NotAuthorised(
            f"Your role ({Role.LABELS.get(getattr(user, 'role', None), 'unknown')}) "
            f"is not allowed to {action or 'do this'}."
        )


# --- Password hashing ---------------------------------------------------------------

# A throwaway hash to verify against when the account does not exist, so the not-found
# path costs the same PBKDF2 time as a real wrong-password check and login latency does
# not reveal which usernames exist. Computed once at import.
_DUMMY_HASH = None


def dummy_verify(password: str = "x") -> bool:
    """Spend one PBKDF2 verification without revealing anything. Always returns False."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")
    verify_password(password or "x", _DUMMY_HASH)
    return False


def hash_password(password: str, iterations: int = None) -> str:
    """Return `pbkdf2_sha256$iterations$salt$hash`."""
    if not password:
        raise ValueError("Password cannot be empty.")
    iterations = iterations or config.PBKDF2_ITERATIONS
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (
        HASH_SCHEME,
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    if not password or not stored:
        return False
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != HASH_SCHEME:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations)
    )
    return hmac.compare_digest(candidate, expected)


def generate_temp_password(length: int = 10) -> str:
    """Readable one-time password for a newly created user."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- Login --------------------------------------------------------------------------

def authenticate(username: str, password: str) -> User:
    """Verify credentials and return the User, or raise AuthError.

    Failure messages stay deliberately vague so the screen can't be used to discover
    which usernames exist.
    """
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("Enter both a username and a password.")

    user = User.get_or_none(User.username == username)
    if user is None:
        # Equalize timing with the real path (which runs PBKDF2) so response latency
        # cannot be used to tell whether a username exists.
        dummy_verify(password)
        raise AuthError("Incorrect username or password.")

    now = datetime.datetime.now()
    if user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() // 60) + 1
        raise AuthError(f"Account locked after too many attempts. Try again in {remaining} min.")

    if not user.is_active:
        raise AuthError("This account has been deactivated. Ask an owner to re-enable it.")

    if not verify_password(password, user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= LOCKOUT_THRESHOLD:
            user.locked_until = now + datetime.timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_attempts = 0
            user.save()
            record_audit(None, "LOGIN_LOCKED", summary=f"Locked out '{username}'",
                         username=username)
            raise AuthError(
                f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
            )
        user.save()
        # Every failure is recorded: a burst of these is what a brute-force looks like.
        record_audit(None, "LOGIN_FAILED", username=username,
                     summary=f"Failed sign-in for '{username}' "
                             f"(attempt {user.failed_attempts} of {LOCKOUT_THRESHOLD})")
        raise AuthError("Incorrect username or password.")

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    # Re-hash if the stored work factor is below what we now require. The password is
    # only in memory at this moment, so this is the one chance to upgrade it.
    if stored_iterations(user.password_hash) < config.PBKDF2_ITERATIONS:
        user.password_hash = hash_password(password)
    user.save()
    record_audit(user, "LOGIN", summary=f"{user.full_name} signed in")
    return user


def stored_iterations(stored) -> int:
    """The work factor a stored hash was created with, or 0 if unreadable."""
    try:
        scheme, iterations, _, _ = (stored or "").split("$")
        return int(iterations) if scheme == HASH_SCHEME else 0
    except (ValueError, TypeError):
        return 0


def change_password(user: User, new_password: str, current_password: str = None,
                    require_current: bool = True):
    if require_current and not verify_password(current_password or "", user.password_hash):
        raise AuthError("Your current password is incorrect.")
    problem = password_problem(new_password)
    if problem:
        raise AuthError(problem)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.save()
    record_audit(user, "PASSWORD_CHANGE", summary=f"{user.full_name} changed their password")


def password_problem(password: str):
    """Return a human-readable reason the password is unacceptable, or None."""
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    if password.isdigit():
        return "Password cannot be only numbers."
    if password.lower() in {"password", "12345678", "inventory", "admin123"}:
        return "That password is too common. Choose another."
    return None


# --- Audit --------------------------------------------------------------------------

def sync_user_directory(user):
    """Keep the platform username directory in step when a tenant user is created.

    A no-op on single-tenant SQLite. Called after creating or renaming a User so the one
    shared login page can route the username to the right tenant. Import is local to
    avoid a cycle (tenancy imports auth for password helpers).
    """
    try:
        from services import tenancy
        if not tenancy.is_multitenant():
            return
        tenant = tenancy.current_tenant_from_search_path()
        if tenant is not None:
            tenancy.register_username(user.username, tenant)
    except Exception:  # pragma: no cover - directory sync must not break user creation
        log.exception("Could not sync the user directory for '%s'",
                      getattr(user, "username", "?"))


def record_audit(user, action, entity=None, entity_id=None, summary=None, detail=None,
                 username=None):
    """Best-effort audit write; never let logging break the operation it describes."""
    try:
        AuditLog.create(
            user=user,
            username=username or (user.username if user else None),
            action=action,
            entity=entity,
            entity_id=entity_id,
            summary=summary,
            detail=detail,
        )
    except Exception:  # pragma: no cover - auditing must not raise
        pass
