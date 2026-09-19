"""Web payment entry for sales orders."""
import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request

from database.models import PaymentStatus, SalesOrder, SalesStatus
from services import auth, sales
from web.context import (
    check_csrf,
    forbidden,
    redirect,
    render,
    require_login,
    resolve_user,
)

router = APIRouter()

PAYMENT_METHODS = (
    "CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD", "CREDIT NOTE",
)


def _signed_in(request):
    context = resolve_user(request)
    return context, require_login(context)


def _payment_date(raw):
    try:
        return datetime.date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValueError("Payment date must be a valid date.")


def _payment_amount(raw):
    try:
        amount = Decimal((raw or "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Payment amount must be a number.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Payment amount cannot have more than two decimal places.")
    return amount


def _order(context, order_id):
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return None, render(
            context, "error.html", status_code=404,
            title="Sales order not found",
            message="That sales order no longer exists.",
        )
    return order, None


def _payment_page(context, order, *, values=None, error=None, status_code=200):
    values = values or {
        "amount": str(order.balance_due),
        "method": "UPI",
        "reference": "",
        "payment_date": datetime.date.today().isoformat(),
        "notes": "",
        "allow_overpayment": False,
    }
    return render(
        context, "payment_form.html", status_code=status_code,
        order=order, values=values, methods=PAYMENT_METHODS, error=error,
    )


def _cannot_take_payment(context, order):
    if order.status == SalesStatus.CANCELLED:
        return render(
            context, "error.html", status_code=400,
            title="Payment not available",
            message="Payments cannot be recorded against a cancelled order.",
        )
    if order.payment_status == PaymentStatus.PAID or order.balance_due <= 0:
        return render(
            context, "error.html", status_code=400,
            title="Order already paid",
            message=f"{order.number} has no outstanding balance.",
        )
    return None


@router.get("/shipping/{order_id}/payments/new")
def payment_new(request: Request, order_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECORD_PAYMENT):
        return forbidden(context, "record payments")
    order, problem = _order(context, order_id)
    if problem is not None:
        return problem
    unavailable = _cannot_take_payment(context, order)
    if unavailable is not None:
        return unavailable
    return _payment_page(context, order)


@router.post("/shipping/{order_id}/payments")
def payment_create(
    request: Request, order_id: int, amount: str = Form(""),
    method: str = Form("UPI"), reference: str = Form(""),
    payment_date: str = Form(""), notes: str = Form(""),
    allow_overpayment: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECORD_PAYMENT):
        return forbidden(context, "record payments")
    order, problem = _order(context, order_id)
    if problem is not None:
        return problem
    values = {
        "amount": amount,
        "method": method,
        "reference": reference,
        "payment_date": payment_date,
        "notes": notes,
        "allow_overpayment": bool(allow_overpayment),
    }
    if not check_csrf(context, csrf_token):
        return _payment_page(
            context, order, values=values,
            error="The form expired. Try again.", status_code=400,
        )
    unavailable = _cannot_take_payment(context, order)
    if unavailable is not None:
        return unavailable
    try:
        payment_method = (method or "").strip().upper()
        if payment_method not in PAYMENT_METHODS:
            raise ValueError("Choose a valid payment method.")
        payment = sales.record_payment(
            order,
            _payment_amount(amount),
            user=context.user,
            method=payment_method,
            reference=(reference or "").strip() or None,
            payment_date=_payment_date(payment_date),
            notes=(notes or "").strip() or None,
            allow_overpayment=bool(allow_overpayment),
        )
    except (ValueError, sales.SalesError) as exc:
        return _payment_page(
            context, order, values=values, error=str(exc), status_code=400)
    return redirect(context, f"/shipping/{order.id}?saved={payment.number}")
