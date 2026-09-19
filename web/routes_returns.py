"""Customer and supplier return workflows for the web application."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from peewee import JOIN, prefetch

from database.models import (
    Customer,
    CustomerReturn,
    CustomerReturnLine,
    Item,
    PurchaseOrder,
    PurchaseStatus,
    SalesOrder,
    SalesStatus,
    Supplier,
    SupplierReturn,
    SupplierReturnLine,
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


def _record(model, raw, label):
    try:
        return model.get_by_id(int(raw))
    except (TypeError, ValueError, model.DoesNotExist):
        raise ValueError(f"Choose a valid {label}.")


def _optional_record(model, raw, label):
    return None if not (raw or "").strip() else _record(model, raw, label)


def _decimal(raw, label, *, optional=False):
    raw = (raw or "").strip()
    if not raw and optional:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a number.")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} cannot be negative.")
    return value


def _serials(raw):
    values = [
        part.strip() for part in (raw or "").replace("\r", "\n")
        .replace(",", "\n").split("\n") if part.strip()
    ]
    if len(values) != len(set(values)):
        raise ValueError("A serial number can appear only once on a return line.")
    return values


def _rows(item_ids, quantities, values, serial_values):
    size = max(len(item_ids), len(quantities), len(values), len(serial_values), 5)
    return [{
        "item_id": item_ids[index] if index < len(item_ids) else "",
        "quantity": quantities[index] if index < len(quantities) else "",
        "value": values[index] if index < len(values) else "",
        "serials": serial_values[index] if index < len(serial_values) else "",
    } for index in range(size)]


def _blank_rows():
    return _rows([], [], [], [])


def _order_rows(order, value_field):
    if order is None:
        return _blank_rows()
    rows = [{
        "item_id": str(line.item_id), "quantity": "",
        "value": str(getattr(line, value_field)), "serials": "",
    } for line in order.lines]
    while len(rows) < 5:
        rows.append({"item_id": "", "quantity": "", "value": "", "serials": ""})
    return rows


def _business_lines(rows, value_key):
    result = []
    seen = set()
    for row in rows:
        item_raw = (row["item_id"] or "").strip()
        quantity_raw = (row["quantity"] or "").strip()
        if not item_raw and not quantity_raw:
            continue
        if not item_raw:
            raise ValueError("Choose an item for every entered quantity.")
        item = _record(Item, item_raw, "item")
        if item.id in seen:
            raise ValueError(f"{item.name} appears more than once. Combine it into one line.")
        seen.add(item.id)
        quantity = _decimal(quantity_raw, f"Quantity for {item.name}")
        if quantity <= 0:
            raise ValueError(f"Quantity for {item.name} must be greater than zero.")
        value = _decimal(row["value"], f"Value for {item.name}", optional=True)
        entry = {"item": item, "quantity": quantity, "serials": _serials(row["serials"])}
        if value is not None:
            entry[value_key] = value
        result.append(entry)
    if not result:
        raise ValueError("Add at least one item with a quantity.")
    return result


def _customer_orders():
    return list(
        SalesOrder.select(SalesOrder, Customer, Warehouse)
        .join(Customer).switch(SalesOrder).join(Warehouse)
        .where(SalesOrder.status.in_((SalesStatus.PARTIAL, SalesStatus.SHIPPED)))
        .order_by(SalesOrder.id.desc()).limit(300)
    )


def _supplier_orders():
    return list(
        PurchaseOrder.select(PurchaseOrder, Supplier, Warehouse)
        .join(Supplier).switch(PurchaseOrder).join(Warehouse)
        .where(PurchaseOrder.status.in_((PurchaseStatus.PARTIAL, PurchaseStatus.RECEIVED)))
        .order_by(PurchaseOrder.id.desc()).limit(300)
    )


def _customer_form_page(context, *, order=None, values=None, lines=None,
                        error=None, status_code=200):
    values = values or {
        "customer_id": str(order.customer_id) if order else "",
        "warehouse_id": str(order.warehouse_id) if order else "",
        "order_id": str(order.id) if order else "",
        "reason": "", "disposition": "restock", "notes": "",
    }
    return render(
        context, "return_form.html", status_code=status_code, kind="customer",
        values=values, lines=lines or _order_rows(order, "unit_price"), error=error,
        partners=list(Customer.select().where(
            Customer.is_active == True).order_by(Customer.name)),  # noqa: E712
        orders=_customer_orders(),
        warehouses=list(Warehouse.select().where(
            Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
        items=list(Item.select().where(
            Item.is_active == True).order_by(Item.name)),  # noqa: E712
    )


def _supplier_form_page(context, *, order=None, values=None, lines=None,
                        error=None, status_code=200):
    values = values or {
        "partner_id": str(order.supplier_id) if order else "",
        "warehouse_id": str(order.warehouse_id) if order else "",
        "order_id": str(order.id) if order else "",
        "reason": "", "notes": "",
    }
    return render(
        context, "return_form.html", status_code=status_code, kind="supplier",
        values=values, lines=lines or _order_rows(order, "unit_cost"), error=error,
        partners=list(Supplier.select().where(
            Supplier.is_active == True).order_by(Supplier.name)),  # noqa: E712
        orders=_supplier_orders(),
        warehouses=list(Warehouse.select().where(
            Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
        items=list(Item.select().where(
            Item.is_active == True).order_by(Item.name)),  # noqa: E712
    )


def _customer_returns():
    base = (CustomerReturn
            .select(CustomerReturn, Customer, Warehouse, SalesOrder)
            .join(Customer)
            .switch(CustomerReturn).join(Warehouse)
            .switch(CustomerReturn).join(SalesOrder, JOIN.LEFT_OUTER)
            .order_by(CustomerReturn.id.desc()).limit(200))
    return list(prefetch(base, CustomerReturnLine.select()))


def _supplier_returns():
    base = (SupplierReturn
            .select(SupplierReturn, Supplier, Warehouse, PurchaseOrder)
            .join(Supplier)
            .switch(SupplierReturn).join(Warehouse)
            .switch(SupplierReturn).join(PurchaseOrder, JOIN.LEFT_OUTER)
            .order_by(SupplierReturn.id.desc()).limit(200))
    return list(prefetch(base, SupplierReturnLine.select()))


@router.get("/operations/returns")
def returns_list(request: Request, kind: str = "customer", saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    kind = kind if kind in ("customer", "supplier") else "customer"
    permission = auth.PERM_CREATE_SALES if kind == "customer" else auth.PERM_RECEIVE_GOODS
    if not context.can(permission):
        return forbidden(context, "view returns")
    return render(
        context, "returns.html", kind=kind, saved=(saved or "").strip(),
        returns=_customer_returns() if kind == "customer" else _supplier_returns(),
    )


@router.get("/operations/returns/customer/new")
def customer_return_new(request: Request, order: int = 0):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_SALES):
        return forbidden(context, "accept customer returns")
    linked = SalesOrder.get_or_none(SalesOrder.id == order) if order else None
    return _customer_form_page(context, order=linked)


@router.post("/operations/returns/customer")
def customer_return_create(
    request: Request, customer_id: str = Form(""), warehouse_id: str = Form(""),
    order_id: str = Form(""), reason: str = Form(""),
    disposition: str = Form("restock"), notes: str = Form(""),
    item_id: list[str] = Form([]), quantity: list[str] = Form([]),
    value: list[str] = Form([]), serials: list[str] = Form([]),
    csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_SALES):
        return forbidden(context, "accept customer returns")
    values = {
        "customer_id": customer_id, "warehouse_id": warehouse_id,
        "order_id": order_id, "reason": reason, "disposition": disposition,
        "notes": notes,
    }
    rows = _rows(item_id, quantity, value, serials)
    linked = None
    try:
        linked = _optional_record(SalesOrder, order_id, "sales order")
    except ValueError:
        pass
    if not check_csrf(context, csrf_token):
        return _customer_form_page(
            context, order=linked, values=values, lines=rows,
            error="The form expired. Try again.", status_code=400)
    try:
        if disposition not in ("restock", "scrap"):
            raise ValueError("Choose whether returned goods are restocked or scrapped.")
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("Enter the reason for this return.")
        record = sales.create_customer_return(
            _record(Customer, customer_id, "customer"),
            _record(Warehouse, warehouse_id, "warehouse"),
            _business_lines(rows, "unit_price"), user=context.user,
            order=_optional_record(SalesOrder, order_id, "sales order"),
            reason=reason, restock=disposition == "restock",
            notes=(notes or "").strip() or None,
        )
    except (ValueError, sales.SalesError, inventory.StockError) as exc:
        return _customer_form_page(
            context, order=linked, values=values, lines=rows,
            error=str(exc), status_code=400)
    return redirect(context, f"/operations/returns/customer/{record.id}")


@router.get("/operations/returns/customer/{return_id}")
def customer_return_detail(request: Request, return_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CREATE_SALES):
        return forbidden(context, "view customer returns")
    record = CustomerReturn.get_or_none(CustomerReturn.id == return_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Return not found", message="That customer return no longer exists.")
    return render(context, "return_detail.html", kind="customer", record=record,
                  lines=list(record.lines))


@router.get("/operations/returns/supplier/new")
def supplier_return_new(request: Request, order: int = 0):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECEIVE_GOODS):
        return forbidden(context, "return goods to suppliers")
    linked = PurchaseOrder.get_or_none(PurchaseOrder.id == order) if order else None
    return _supplier_form_page(context, order=linked)


@router.post("/operations/returns/supplier")
def supplier_return_create(
    request: Request, partner_id: str = Form(""), warehouse_id: str = Form(""),
    order_id: str = Form(""), reason: str = Form(""), notes: str = Form(""),
    item_id: list[str] = Form([]), quantity: list[str] = Form([]),
    value: list[str] = Form([]), serials: list[str] = Form([]),
    csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECEIVE_GOODS):
        return forbidden(context, "return goods to suppliers")
    values = {
        "partner_id": partner_id, "warehouse_id": warehouse_id,
        "order_id": order_id, "reason": reason, "notes": notes,
    }
    rows = _rows(item_id, quantity, value, serials)
    linked = None
    try:
        linked = _optional_record(PurchaseOrder, order_id, "purchase order")
    except ValueError:
        pass
    if not check_csrf(context, csrf_token):
        return _supplier_form_page(
            context, order=linked, values=values, lines=rows,
            error="The form expired. Try again.", status_code=400)
    try:
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("Enter the reason for this return.")
        record = purchasing.create_supplier_return(
            _record(Supplier, partner_id, "supplier"),
            _record(Warehouse, warehouse_id, "warehouse"),
            _business_lines(rows, "unit_cost"), user=context.user,
            order=_optional_record(PurchaseOrder, order_id, "purchase order"),
            reason=reason, notes=(notes or "").strip() or None,
        )
    except (ValueError, purchasing.PurchasingError, inventory.StockError) as exc:
        return _supplier_form_page(
            context, order=linked, values=values, lines=rows,
            error=str(exc), status_code=400)
    return redirect(context, f"/operations/returns/supplier/{record.id}")


@router.get("/operations/returns/supplier/{return_id}")
def supplier_return_detail(request: Request, return_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_RECEIVE_GOODS):
        return forbidden(context, "view supplier returns")
    record = SupplierReturn.get_or_none(SupplierReturn.id == return_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Return not found", message="That supplier return no longer exists.")
    return render(context, "return_detail.html", kind="supplier", record=record,
                  lines=list(record.lines))
