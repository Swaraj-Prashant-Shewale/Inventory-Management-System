"""Authenticated PDF downloads for operational and statutory documents."""
import logging
import os
import tempfile

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from database.models import Fulfillment, GoodsReceipt, Invoice, Item, PurchaseOrder, SalesOrder
from services import auth, documents, sales
from web import security
from web.context import (
    check_csrf,
    forbidden,
    redirect,
    render,
    require_login,
    resolve_user,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _signed_in(request, permission, action):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return context, blocked
    if not context.can(permission):
        return context, forbidden(context, action)
    return context, None


def _remove_file(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _pdf_response(context, builder, record, filename):
    """Build one PDF in the platform temp directory and delete it after streaming."""
    handle, path = tempfile.mkstemp(prefix="ims_document_", suffix=".pdf")
    os.close(handle)
    try:
        builder(record, path=path)
    except documents.DocumentError as exc:
        _remove_file(path)
        return render(
            context, "error.html", status_code=400,
            title="Document unavailable", message=str(exc),
        )
    except Exception:
        _remove_file(path)
        log.exception("Could not generate %s", filename)
        return render(
            context, "error.html", status_code=500,
            title="Document unavailable",
            message="The PDF could not be generated. The error has been logged.",
        )

    response = FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        background=BackgroundTask(_remove_file, path),
    )
    response.headers["Cache-Control"] = "private, no-store"
    if context.fresh_cookie:
        security.set_cookie(response, security.SESSION_COOKIE, context.fresh_cookie)
    return response


def _not_found(context, label):
    return render(
        context, "error.html", status_code=404,
        title=f"{label} not found",
        message=f"That {label.lower()} no longer exists.",
    )


@router.get("/documents/purchase-orders/{order_id}.pdf")
def purchase_order_pdf(request: Request, order_id: int):
    context, blocked = _signed_in(
        request, auth.PERM_CREATE_PURCHASE, "download purchase orders")
    if blocked is not None:
        return blocked
    order = PurchaseOrder.get_or_none(PurchaseOrder.id == order_id)
    if order is None:
        return _not_found(context, "Purchase order")
    return _pdf_response(
        context, documents.purchase_order, order,
        f"{documents.safe_name(order.number)}.pdf",
    )


@router.get("/documents/goods-receipts/{receipt_id}.pdf")
def goods_receipt_pdf(request: Request, receipt_id: int):
    context, blocked = _signed_in(
        request, auth.PERM_RECEIVE_GOODS, "download goods receipt notes")
    if blocked is not None:
        return blocked
    receipt = GoodsReceipt.get_or_none(GoodsReceipt.id == receipt_id)
    if receipt is None:
        return _not_found(context, "Goods receipt")
    return _pdf_response(
        context, documents.goods_receipt_note, receipt,
        f"{documents.safe_name(receipt.number)}.pdf",
    )


@router.get("/documents/delivery-challans/{fulfillment_id}.pdf")
def delivery_challan_pdf(request: Request, fulfillment_id: int):
    context, blocked = _signed_in(
        request, auth.PERM_FULFILL_GOODS, "download delivery challans")
    if blocked is not None:
        return blocked
    fulfillment = Fulfillment.get_or_none(Fulfillment.id == fulfillment_id)
    if fulfillment is None:
        return _not_found(context, "Shipment")
    return _pdf_response(
        context, documents.delivery_challan, fulfillment,
        f"{documents.safe_name(fulfillment.number)}-challan.pdf",
    )


@router.get("/documents/invoices/{invoice_id}.pdf")
def tax_invoice_pdf(request: Request, invoice_id: int):
    context, blocked = _signed_in(
        request, auth.PERM_CREATE_SALES, "download tax invoices")
    if blocked is not None:
        return blocked
    invoice = Invoice.get_or_none(Invoice.id == invoice_id)
    if invoice is None:
        return _not_found(context, "Invoice")
    return _pdf_response(
        context, documents.tax_invoice, invoice,
        f"{documents.safe_name(invoice.number)}.pdf",
    )


@router.post("/shipping/{order_id}/invoice")
def create_invoice(request: Request, order_id: int, csrf_token: str = Form("")):
    context, blocked = _signed_in(
        request, auth.PERM_CREATE_SALES, "raise tax invoices")
    if blocked is not None:
        return blocked
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return _not_found(context, "Sales order")
    if not check_csrf(context, csrf_token):
        return render(
            context, "error.html", status_code=400,
            title="Form expired", message="Reload the sales order and try again.",
        )
    invoice = sales.invoice_for(order)
    try:
        if invoice is None:
            invoice = sales.create_invoice(order, user=context.user)
    except auth.NotAuthorised:
        return forbidden(context, "raise tax invoices")
    except sales.SalesError as exc:
        return render(
            context, "error.html", status_code=400,
            title="Invoice not created", message=str(exc),
        )
    return redirect(context, f"/shipping/{order.id}?saved={invoice.number}")


def _labels_page(context, *, selected=None, copies="1", include_price=False,
                 error=None, status_code=200):
    return render(
        context, "label_form.html", status_code=status_code,
        items=list(Item.select().where(
            Item.is_active == True).order_by(Item.name)),  # noqa: E712
        selected=set(selected or []), copies=copies,
        include_price=include_price, error=error,
    )


@router.get("/documents/labels")
def labels_page(request: Request):
    context, blocked = _signed_in(
        request, auth.PERM_VIEW_INVENTORY, "print item labels")
    if blocked is not None:
        return blocked
    return _labels_page(context)


@router.post("/documents/labels.pdf")
def labels_pdf(
    request: Request, item_id: list[str] = Form([]), copies: str = Form("1"),
    include_price: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(
        request, auth.PERM_VIEW_INVENTORY, "print item labels")
    if blocked is not None:
        return blocked
    selected = []
    for raw in item_id:
        try:
            selected.append(int(raw))
        except (TypeError, ValueError):
            pass
    show_price = bool(include_price) and context.can(auth.PERM_MANAGE_PRICING)
    if not check_csrf(context, csrf_token):
        return _labels_page(
            context, selected=selected, copies=copies, include_price=show_price,
            error="The form expired. Try again.", status_code=400,
        )
    try:
        copy_count = int(copies)
    except (TypeError, ValueError):
        copy_count = 0
    if copy_count < 1 or copy_count > 10:
        return _labels_page(
            context, selected=selected, copies=copies, include_price=show_price,
            error="Copies must be between 1 and 10.", status_code=400,
        )
    if not selected:
        return _labels_page(
            context, selected=selected, copies=copies, include_price=show_price,
            error="Select at least one item.", status_code=400,
        )
    if len(selected) > 100:
        return _labels_page(
            context, selected=selected, copies=copies, include_price=show_price,
            error="Select no more than 100 items at a time.", status_code=400,
        )
    items = list(Item.select().where(
        (Item.id.in_(selected)) & (Item.is_active == True)).order_by(Item.name))  # noqa: E712
    if not items:
        return _labels_page(
            context, selected=selected, copies=copies, include_price=show_price,
            error="The selected items are no longer available.", status_code=400,
        )
    return _pdf_response(
        context,
        lambda records, path: documents.barcode_labels(
            records, path=path, copies=copy_count, include_price=show_price),
        items,
        "item-labels.pdf",
    )
