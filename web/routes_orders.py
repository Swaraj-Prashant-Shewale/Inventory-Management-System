"""W2 receiving and shipping routes backed by the shared business services."""
import datetime
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from peewee import JOIN, IntegrityError

from database.models import (
    Customer,
    Fulfillment,
    GoodsReceipt,
    Invoice,
    Item,
    Payment,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseStatus,
    SalesOrder,
    SalesOrderLine,
    SalesStatus,
    Supplier,
    Warehouse,
)
from services import auth, inventory, purchasing, sales
from web.context import (
    check_csrf,
    forbidden,
    redirect,
    render,
    require_login,
    resolve_user,
)

router = APIRouter()


def _signed_in(request):
    context = resolve_user(request)
    return context, require_login(context)


def _date(raw, label, *, optional=True):
    raw = (raw or "").strip()
    if not raw and optional:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} must be a valid date.")


def _decimal(raw, label, *, allow_blank=False):
    raw = (raw or "").strip()
    if not raw and allow_blank:
        return None
    try:
        value = Decimal(raw or "0")
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.")
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")
    return value


def _record(model, raw, label):
    try:
        return model.get_by_id(int(raw))
    except (ValueError, TypeError, model.DoesNotExist):
        raise ValueError(f"Choose a valid {label}.")


def _split_serials(raw):
    return [part.strip() for part in re.split(r"[\r\n,]+", raw or "") if part.strip()]


def _blank_lines(count=5):
    return [dict(item_id="", quantity="", price="", discount="")
            for _ in range(count)]


def _submitted_lines(item_ids, quantities, prices, discounts=None):
    discounts = discounts or [""] * len(item_ids)
    size = max(len(item_ids), len(quantities), len(prices), len(discounts), 5)
    rows = []
    for index in range(size):
        rows.append({
            "item_id": item_ids[index] if index < len(item_ids) else "",
            "quantity": quantities[index] if index < len(quantities) else "",
            "price": prices[index] if index < len(prices) else "",
            "discount": discounts[index] if index < len(discounts) else "",
        })
    return rows


def _business_lines(rows, *, sales_order=False):
    result = []
    seen = set()
    for row in rows:
        item_raw = (row.get("item_id") or "").strip()
        qty_raw = (row.get("quantity") or "").strip()
        if not item_raw and not qty_raw:
            continue
        if not item_raw:
            raise ValueError("Choose an item for every entered quantity.")
        item = _record(Item, item_raw, "item")
        if item.id in seen:
            raise ValueError(f"{item.name} appears more than once. Combine it into one line.")
        seen.add(item.id)
        quantity = _decimal(qty_raw, f"Quantity for {item.name}")
        if quantity <= 0:
            raise ValueError(f"Quantity for {item.name} must be greater than zero.")
        price = _decimal(row.get("price"),
                         f"{'Price' if sales_order else 'Cost'} for {item.name}",
                         allow_blank=True)
        if sales_order:
            discount = _decimal(row.get("discount"),
                                f"Discount for {item.name}", allow_blank=True)
            if discount is not None and discount > 100:
                raise ValueError(f"Discount for {item.name} cannot exceed 100%.")
            result.append({
                "item": item, "quantity": quantity, "unit_price": price,
                "discount_percent": discount or Decimal("0"),
            })
        else:
            result.append({"item": item, "quantity": quantity, "unit_cost": price})
    if not result:
        raise ValueError("Add at least one item with a quantity.")
    return result


def _purchase_form_page(context, *, values=None, lines=None, error=None,
                        status_code=200):
    values = values or {"supplier_id": "", "warehouse_id": "",
                        "expected_date": "", "notes": ""}
    return render(
        context, "purchase_form.html", status_code=status_code,
        values=values, lines=lines or _blank_lines(), error=error,
        suppliers=list(Supplier.select().where(
            Supplier.is_active == True).order_by(Supplier.name)),  # noqa: E712
        warehouses=list(Warehouse.select().where(
            Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
        items=list(Item.select().where(
            Item.is_active == True).order_by(Item.name)),  # noqa: E712
    )


def _sales_form_page(context, *, values=None, lines=None, error=None,
                     status_code=200):
    values = values or {"customer_id": "", "warehouse_id": "",
                        "promised_date": "", "truck_hsrp": "",
                        "delivery_address": "", "delivery_pincode": "", "notes": ""}
    return render(
        context, "sales_form.html", status_code=status_code,
        values=values, lines=lines or _blank_lines(), error=error,
        customers=list(Customer.select().where(
            Customer.is_active == True).order_by(Customer.name)),  # noqa: E712
        warehouses=list(Warehouse.select().where(
            Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
        items=list(Item.select().where(
            Item.is_active == True).order_by(Item.name)),  # noqa: E712
    )


@router.get("/receiving")
def receiving(request: Request, q: str = "", status: str = "", saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    query = (PurchaseOrder.select(PurchaseOrder, Supplier, Warehouse)
             .join(Supplier)
             .switch(PurchaseOrder).join(Warehouse))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.where((PurchaseOrder.number ** like) | (Supplier.name ** like))
    if status in PurchaseStatus.LABELS:
        query = query.where(PurchaseOrder.status == status)
    else:
        status = ""
    orders = list(query.order_by(PurchaseOrder.order_date.desc(),
                                 PurchaseOrder.id.desc()).limit(300))
    return render(context, "receiving.html", orders=orders, q=term, status=status,
                  saved=(saved or "").strip(), PurchaseStatus=PurchaseStatus)


@router.get("/receiving/suppliers/new")
def supplier_new_page(request: Request):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_PARTNERS):
        return forbidden(context, "create suppliers")
    return render(context, "partner_form.html", kind="supplier", values={}, error=None)


@router.post("/receiving/suppliers/new")
def supplier_create(
    request: Request, code: str = Form(""), name: str = Form(""),
    gstin: str = Form(""), state: str = Form(""), state_code: str = Form(""),
    contact_person: str = Form(""), phone: str = Form(""), email: str = Form(""),
    city: str = Form(""), address: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_PARTNERS):
        return forbidden(context, "create suppliers")
    values = locals().copy()
    if not check_csrf(context, csrf_token):
        return render(context, "partner_form.html", kind="supplier", values=values,
                      error="The form expired. Try again.", status_code=400)
    code, name = (code or "").strip().upper(), (name or "").strip()
    error = _partner_error(Supplier, code, name, state_code, gstin)
    if error:
        return render(context, "partner_form.html", kind="supplier", values=values,
                      error=error, status_code=400)
    try:
        record = Supplier.create(
            code=code, name=name, gstin=(gstin or "").strip().upper() or None,
            state=(state or "").strip() or None,
            state_code=(state_code or "").strip() or None,
            contact_person=(contact_person or "").strip() or None,
            phone=(phone or "").strip() or None, email=(email or "").strip() or None,
            city=(city or "").strip() or None,
            address_line1=(address or "").strip() or None,
        )
    except IntegrityError:
        return render(context, "partner_form.html", kind="supplier", values=values,
                      error="That supplier code is already in use.", status_code=400)
    auth.record_audit(context.user, "CREATE", "Supplier", record.id,
                      f"Created supplier {record.code} - {record.name}")
    return redirect(context, f"/receiving/orders/new?supplier={record.id}")


@router.get("/receiving/orders/new")
def purchase_new_page(request: Request, supplier: int = 0):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_PURCHASE):
        return forbidden(context, "create purchase orders")
    values = {"supplier_id": str(supplier or ""), "warehouse_id": "",
              "expected_date": "", "notes": ""}
    return _purchase_form_page(context, values=values)


@router.post("/receiving/orders")
def purchase_create(
    request: Request, supplier_id: str = Form(""), warehouse_id: str = Form(""),
    expected_date: str = Form(""), notes: str = Form(""),
    item_id: list[str] = Form([]), quantity: list[str] = Form([]),
    price: list[str] = Form([]), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_PURCHASE):
        return forbidden(context, "create purchase orders")
    values = {"supplier_id": supplier_id, "warehouse_id": warehouse_id,
              "expected_date": expected_date, "notes": notes}
    rows = _submitted_lines(item_id, quantity, price)
    if not check_csrf(context, csrf_token):
        return _purchase_form_page(context, values=values, lines=rows,
                                   error="The form expired. Try again.", status_code=400)
    try:
        order = purchasing.create_purchase_order(
            _record(Supplier, supplier_id, "supplier"),
            _record(Warehouse, warehouse_id, "warehouse"),
            _business_lines(rows), user=context.user,
            expected_date=_date(expected_date, "Expected date"),
            notes=(notes or "").strip() or None,
        )
    except (ValueError, purchasing.PurchasingError) as exc:
        return _purchase_form_page(context, values=values, lines=rows,
                                   error=str(exc), status_code=400)
    return redirect(context, f"/receiving/{order.id}?saved={order.number}")


def _purchase_detail_page(context, order, *, error=None, saved="", status_code=200):
    return render(context, "purchase_detail.html", status_code=status_code,
                  order=order, lines=list(order.lines),
                  receipts=list(order.receipts.order_by(GoodsReceipt.id.desc())),
                  error=error, saved=saved, PurchaseStatus=PurchaseStatus)


@router.get("/receiving/{order_id}")
def purchase_detail(request: Request, order_id: int, saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    order = PurchaseOrder.get_or_none(PurchaseOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That purchase order no longer exists.")
    return _purchase_detail_page(context, order, saved=(saved or "").strip())


@router.post("/receiving/{order_id}/confirm")
def purchase_confirm(request: Request, order_id: int, csrf_token: str = Form("")):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    order = PurchaseOrder.get_or_none(PurchaseOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That purchase order no longer exists.")
    if not check_csrf(context, csrf_token):
        return _purchase_detail_page(context, order, error="The form expired. Try again.",
                                     status_code=400)
    try:
        purchasing.confirm_order(order, user=context.user)
    except auth.NotAuthorised:
        return forbidden(context, "confirm purchase orders")
    except purchasing.PurchasingError as exc:
        return _purchase_detail_page(context, order, error=str(exc), status_code=400)
    return redirect(context, f"/receiving/{order.id}?saved=Order%20confirmed")


def _receipt_page(context, order, *, error=None, status_code=200):
    return render(context, "receive_order.html", status_code=status_code,
                  order=order, lines=purchasing.outstanding_lines(order),
                  today=datetime.date.today().isoformat(), error=error)


@router.get("/receiving/{order_id}/receive")
def receipt_page(request: Request, order_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECEIVE_GOODS):
        return forbidden(context, "receive goods")
    order = PurchaseOrder.get_or_none(PurchaseOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That purchase order no longer exists.")
    if order.status == PurchaseStatus.DRAFT:
        return _purchase_detail_page(context, order,
                                     error="Confirm this order before receiving goods.",
                                     status_code=400)
    return _receipt_page(context, order)


@router.post("/receiving/{order_id}/receive")
def receipt_create(
    request: Request, order_id: int, line_id: list[str] = Form([]),
    quantity: list[str] = Form([]), price: list[str] = Form([]),
    rejected: list[str] = Form([]), lot_number: list[str] = Form([]),
    expiry_date: list[str] = Form([]), serials: list[str] = Form([]),
    receipt_date: str = Form(""), truck_hsrp: str = Form(""),
    supplier_invoice_no: str = Form(""), notes: str = Form(""),
    csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECEIVE_GOODS):
        return forbidden(context, "receive goods")
    order = PurchaseOrder.get_or_none(PurchaseOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That purchase order no longer exists.")
    if order.status == PurchaseStatus.DRAFT:
        return _purchase_detail_page(context, order,
                                     error="Confirm this order before receiving goods.",
                                     status_code=400)
    if not check_csrf(context, csrf_token):
        return _receipt_page(context, order, error="The form expired. Try again.",
                             status_code=400)
    try:
        entries = []
        for index, raw_id in enumerate(line_id):
            qty = _decimal(quantity[index] if index < len(quantity) else "",
                           "Received quantity", allow_blank=True)
            if not qty:
                continue
            line = _record(PurchaseOrderLine, raw_id, "order line")
            unit_cost = _decimal(price[index] if index < len(price) else "",
                                 f"Cost for {line.item.name}", allow_blank=True)
            if unit_cost is None:
                unit_cost = line.unit_cost
            rejected_qty = _decimal(rejected[index] if index < len(rejected) else "",
                                    "Rejected quantity", allow_blank=True) or Decimal("0")
            if rejected_qty > qty:
                raise ValueError(f"Rejected quantity for {line.item.name} exceeds received quantity.")
            entries.append({
                "order_line": line, "quantity": qty,
                "unit_cost": unit_cost,
                "rejected_quantity": rejected_qty,
                "lot_number": lot_number[index] if index < len(lot_number) else "",
                "expiry_date": _date(expiry_date[index] if index < len(expiry_date) else "",
                                     f"Expiry date for {line.item.name}"),
                "serials": _split_serials(serials[index] if index < len(serials) else ""),
            })
        receipt = purchasing.receive_goods(
            order, entries, user=context.user,
            receipt_date=_date(receipt_date, "Receipt date", optional=False),
            truck_hsrp=(truck_hsrp or "").strip() or None,
            supplier_invoice_no=(supplier_invoice_no or "").strip() or None,
            notes=(notes or "").strip() or None,
        )
    except (ValueError, purchasing.PurchasingError, inventory.StockError) as exc:
        return _receipt_page(context, order, error=str(exc), status_code=400)
    return redirect(context, f"/receiving/{order.id}?saved={receipt.number}")


@router.get("/shipping")
def shipping(request: Request, q: str = "", status: str = "", saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    query = (SalesOrder.select(SalesOrder, Customer, Warehouse)
             .join(Customer)
             .switch(SalesOrder).join(Warehouse))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.where((SalesOrder.number ** like) | (Customer.name ** like))
    if status in SalesStatus.LABELS:
        query = query.where(SalesOrder.status == status)
    else:
        status = ""
    orders = list(query.order_by(SalesOrder.order_date.desc(),
                                 SalesOrder.id.desc()).limit(300))
    return render(context, "shipping.html", orders=orders, q=term, status=status,
                  saved=(saved or "").strip(), SalesStatus=SalesStatus)


@router.get("/shipping/customers/new")
def customer_new_page(request: Request):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_PARTNERS):
        return forbidden(context, "create customers")
    return render(context, "partner_form.html", kind="customer", values={}, error=None)


def _partner_error(model, code, name, state_code, gstin):
    if not code or not name:
        return "Code and name are required."
    if len(code) > 30:
        return "Code cannot exceed 30 characters."
    if state_code and (len(state_code.strip()) != 2 or not state_code.strip().isdigit()):
        return "GST state code must contain two digits, such as 27."
    if gstin and len(gstin.strip()) != 15:
        return "GSTIN must contain 15 characters."
    if model.get_or_none(model.code == code):
        return f"Code '{code}' is already in use."
    return None


@router.post("/shipping/customers/new")
def customer_create(
    request: Request, code: str = Form(""), name: str = Form(""),
    gstin: str = Form(""), state: str = Form(""), state_code: str = Form(""),
    contact_person: str = Form(""), phone: str = Form(""), email: str = Form(""),
    city: str = Form(""), address: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_PARTNERS):
        return forbidden(context, "create customers")
    values = locals().copy()
    if not check_csrf(context, csrf_token):
        return render(context, "partner_form.html", kind="customer", values=values,
                      error="The form expired. Try again.", status_code=400)
    code, name = (code or "").strip().upper(), (name or "").strip()
    error = _partner_error(Customer, code, name, state_code, gstin)
    if error:
        return render(context, "partner_form.html", kind="customer", values=values,
                      error=error, status_code=400)
    try:
        record = Customer.create(
            code=code, name=name, gstin=(gstin or "").strip().upper() or None,
            state=(state or "").strip() or None,
            state_code=(state_code or "").strip() or None,
            contact_person=(contact_person or "").strip() or None,
            phone=(phone or "").strip() or None, email=(email or "").strip() or None,
            billing_address=(address or "").strip() or None,
            delivery_address=(address or "").strip() or None,
        )
    except IntegrityError:
        return render(context, "partner_form.html", kind="customer", values=values,
                      error="That customer code is already in use.", status_code=400)
    auth.record_audit(context.user, "CREATE", "Customer", record.id,
                      f"Created customer {record.code} - {record.name}")
    return redirect(context, f"/shipping/orders/new?customer={record.id}")


@router.get("/shipping/orders/new")
def sales_new_page(request: Request, customer: int = 0):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_SALES):
        return forbidden(context, "create sales orders")
    values = {"customer_id": str(customer or ""), "warehouse_id": "",
              "promised_date": "", "truck_hsrp": "", "delivery_address": "",
              "delivery_pincode": "", "notes": ""}
    return _sales_form_page(context, values=values)


@router.post("/shipping/orders")
def sales_create(
    request: Request, customer_id: str = Form(""), warehouse_id: str = Form(""),
    promised_date: str = Form(""), truck_hsrp: str = Form(""),
    delivery_address: str = Form(""), delivery_pincode: str = Form(""),
    notes: str = Form(""), item_id: list[str] = Form([]),
    quantity: list[str] = Form([]), price: list[str] = Form([]),
    discount: list[str] = Form([]), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_SALES):
        return forbidden(context, "create sales orders")
    values = {"customer_id": customer_id, "warehouse_id": warehouse_id,
              "promised_date": promised_date, "truck_hsrp": truck_hsrp,
              "delivery_address": delivery_address,
              "delivery_pincode": delivery_pincode, "notes": notes}
    rows = _submitted_lines(item_id, quantity, price, discount)
    if not check_csrf(context, csrf_token):
        return _sales_form_page(context, values=values, lines=rows,
                                error="The form expired. Try again.", status_code=400)
    try:
        order = sales.create_sales_order(
            _record(Customer, customer_id, "customer"),
            _record(Warehouse, warehouse_id, "warehouse"),
            _business_lines(rows, sales_order=True), user=context.user,
            promised_date=_date(promised_date, "Promised date"),
            truck_hsrp=(truck_hsrp or "").strip() or None,
            delivery_address=(delivery_address or "").strip() or None,
            delivery_pincode=(delivery_pincode or "").strip() or None,
            notes=(notes or "").strip() or None,
        )
    except (ValueError, sales.SalesError) as exc:
        return _sales_form_page(context, values=values, lines=rows,
                                error=str(exc), status_code=400)
    return redirect(context, f"/shipping/{order.id}?saved={order.number}")


def _sales_detail_page(context, order, *, error=None, saved="", status_code=200):
    return render(
        context, "sales_detail.html", status_code=status_code,
        order=order, lines=list(order.lines),
        fulfillments=list(order.fulfillments.order_by(Fulfillment.id.desc())),
        invoices=list(order.invoices.order_by(Invoice.id.desc())),
        payments=list(order.payments.order_by(Payment.id.desc())),
        error=error, saved=saved, SalesStatus=SalesStatus,
    )


@router.get("/shipping/{order_id}")
def sales_detail(request: Request, order_id: int, saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That sales order no longer exists.")
    return _sales_detail_page(context, order, saved=(saved or "").strip())


@router.post("/shipping/{order_id}/confirm")
def sales_confirm(request: Request, order_id: int, csrf_token: str = Form("")):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That sales order no longer exists.")
    if not check_csrf(context, csrf_token):
        return _sales_detail_page(context, order, error="The form expired. Try again.",
                                  status_code=400)
    try:
        sales.confirm_order(order, user=context.user)
    except auth.NotAuthorised:
        return forbidden(context, "confirm sales orders")
    except sales.SalesError as exc:
        return _sales_detail_page(context, order, error=str(exc), status_code=400)
    return redirect(context, f"/shipping/{order.id}?saved=Order%20confirmed")


def _fulfill_page(context, order, *, error=None, status_code=200):
    return render(context, "fulfill_order.html", status_code=status_code,
                  order=order, lines=sales.outstanding_lines(order),
                  today=datetime.date.today().isoformat(), error=error)


@router.get("/shipping/{order_id}/fulfill")
def fulfill_page(request: Request, order_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_FULFILL_GOODS):
        return forbidden(context, "ship goods")
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That sales order no longer exists.")
    if order.status == SalesStatus.DRAFT:
        return _sales_detail_page(context, order,
                                  error="Confirm this order before shipping goods.",
                                  status_code=400)
    return _fulfill_page(context, order)


@router.post("/shipping/{order_id}/fulfill")
def fulfill_create(
    request: Request, order_id: int, line_id: list[str] = Form([]),
    quantity: list[str] = Form([]), serials: list[str] = Form([]),
    ship_date: str = Form(""), truck_hsrp: str = Form(""),
    notes: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_FULFILL_GOODS):
        return forbidden(context, "ship goods")
    order = SalesOrder.get_or_none(SalesOrder.id == order_id)
    if order is None:
        return render(context, "error.html", status_code=404,
                      title="Order not found", message="That sales order no longer exists.")
    if not check_csrf(context, csrf_token):
        return _fulfill_page(context, order, error="The form expired. Try again.",
                             status_code=400)
    try:
        entries = []
        for index, raw_id in enumerate(line_id):
            qty = _decimal(quantity[index] if index < len(quantity) else "",
                           "Ship quantity", allow_blank=True)
            if not qty:
                continue
            entries.append({
                "order_line": _record(SalesOrderLine, raw_id, "order line"),
                "quantity": qty,
                "serials": _split_serials(serials[index] if index < len(serials) else ""),
            })
        fulfillment = sales.fulfill(
            order, entries, user=context.user,
            ship_date=_date(ship_date, "Ship date", optional=False),
            truck_hsrp=(truck_hsrp or "").strip() or None,
            notes=(notes or "").strip() or None,
        )
    except (ValueError, sales.SalesError, inventory.StockError) as exc:
        return _fulfill_page(context, order, error=str(exc), status_code=400)
    return redirect(context, f"/shipping/{order.id}?saved={fulfillment.number}")
