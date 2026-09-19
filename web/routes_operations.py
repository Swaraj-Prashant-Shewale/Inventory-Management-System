"""Remaining W2 warehouse operations: adjustments, transfers, and counts."""
import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from peewee import prefetch

from database.models import (
    AdjustmentReason,
    CountStatus,
    CycleCount,
    CycleCountLine,
    Item,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
    TransferStatus,
    Warehouse,
)
from services import auth, inventory, warehouse_ops
from web.context import (
    check_csrf,
    forbidden,
    redirect,
    render,
    require_login,
    resolve_user,
)

router = APIRouter(prefix="/operations")


def _context(request):
    context = resolve_user(request)
    return context, require_login(context)


def _record(model, raw, label):
    try:
        return model.get_by_id(int(raw))
    except (ValueError, TypeError, model.DoesNotExist):
        raise ValueError(f"Choose a valid {label}.")


def _decimal(raw, label, *, blank=None, signed=False):
    raw = (raw or "").strip()
    if not raw and blank is not None:
        return blank
    try:
        value = Decimal(raw or "0")
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.")
    if not signed and value < 0:
        raise ValueError(f"{label} cannot be negative.")
    return value


def _date(raw, label, *, default=None):
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} must be a valid date.")


def _items():
    return list(Item.select().where(Item.is_active == True).order_by(Item.name))  # noqa: E712


def _warehouses():
    return list(Warehouse.select().where(
        Warehouse.is_active == True).order_by(Warehouse.name))  # noqa: E712


def _line_rows(item_ids=None, quantities=None, notes=None, count=5):
    item_ids, quantities, notes = item_ids or [], quantities or [], notes or []
    size = max(count, len(item_ids), len(quantities), len(notes))
    return [{
        "item_id": item_ids[i] if i < len(item_ids) else "",
        "quantity": quantities[i] if i < len(quantities) else "",
        "note": notes[i] if i < len(notes) else "",
    } for i in range(size)]


def _operation_lines(rows, *, signed=False):
    result, seen = [], set()
    for row in rows:
        item_raw = (row["item_id"] or "").strip()
        qty_raw = (row["quantity"] or "").strip()
        if not item_raw and not qty_raw:
            continue
        if not item_raw:
            raise ValueError("Choose an item for every entered quantity.")
        item = _record(Item, item_raw, "item")
        if item.id in seen:
            raise ValueError(f"{item.name} appears more than once. Combine it into one line.")
        seen.add(item.id)
        quantity = _decimal(qty_raw, f"Quantity for {item.name}", signed=signed)
        if quantity == 0:
            raise ValueError(f"Quantity for {item.name} cannot be zero.")
        result.append({
            "item": item,
            "quantity_delta" if signed else "quantity": quantity,
            "note": (row["note"] or "").strip() or None,
        })
    if not result:
        raise ValueError("Add at least one item with a quantity.")
    return result


def _operations_page(context, tab="", *, error=None, saved="", status_code=200):
    allowed = []
    if context.can(auth.PERM_ADJUST_STOCK):
        allowed.append("adjustments")
    if context.can(auth.PERM_TRANSFER_STOCK):
        allowed.append("transfers")
    if context.can(auth.PERM_CYCLE_COUNT):
        allowed.append("counts")
    if not allowed:
        return forbidden(context, "use warehouse operations")
    if tab not in allowed:
        tab = allowed[0]

    adjustments = transfers = counts = []
    if tab == "adjustments":
        adjustments = list(prefetch(
            StockAdjustment.select().order_by(StockAdjustment.id.desc()).limit(200),
            StockAdjustmentLine,
        ))
    elif tab == "transfers":
        transfers = list(prefetch(
            StockTransfer.select().order_by(StockTransfer.id.desc()).limit(200),
            StockTransferLine,
        ))
    else:
        counts = list(prefetch(
            CycleCount.select().order_by(CycleCount.id.desc()).limit(200),
            CycleCountLine,
        ))
    return render(
        context, "operations.html", status_code=status_code,
        tab=tab, allowed_tabs=allowed, error=error, saved=saved,
        adjustments=adjustments, transfers=transfers, counts=counts,
        warehouses=_warehouses(),
        AdjustmentReason=AdjustmentReason, TransferStatus=TransferStatus,
        CountStatus=CountStatus,
    )


@router.get("")
@router.get("/")
def operations(request: Request, tab: str = "", saved: str = ""):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    return _operations_page(context, tab, saved=(saved or "").strip())


def _adjustment_form(context, *, rows=None, values=None, error=None, status_code=200):
    values = values or {"warehouse_id": "", "reason": AdjustmentReason.OTHER,
                        "adjustment_date": datetime.date.today().isoformat(), "notes": ""}
    return render(context, "adjustment_form.html", status_code=status_code,
                  rows=rows or _line_rows(), values=values, items=_items(),
                  warehouses=_warehouses(), AdjustmentReason=AdjustmentReason,
                  error=error)


@router.get("/adjustments/new")
def adjustment_new(request: Request):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_ADJUST_STOCK):
        return forbidden(context, "create stock adjustments")
    return _adjustment_form(context)


@router.post("/adjustments")
def adjustment_create(
    request: Request, warehouse_id: str = Form(""), reason: str = Form(""),
    adjustment_date: str = Form(""), notes: str = Form(""),
    item_id: list[str] = Form([]), quantity: list[str] = Form([]),
    line_note: list[str] = Form([]), csrf_token: str = Form(""),
):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_ADJUST_STOCK):
        return forbidden(context, "create stock adjustments")
    rows = _line_rows(item_id, quantity, line_note)
    values = {"warehouse_id": warehouse_id, "reason": reason,
              "adjustment_date": adjustment_date, "notes": notes}
    if not check_csrf(context, csrf_token):
        return _adjustment_form(context, rows=rows, values=values,
                                error="The form expired. Try again.", status_code=400)
    try:
        if reason not in AdjustmentReason.ALL:
            raise ValueError("Choose a valid adjustment reason.")
        adjustment = warehouse_ops.create_adjustment(
            _record(Warehouse, warehouse_id, "warehouse"),
            _operation_lines(rows, signed=True), reason=reason, user=context.user,
            notes=(notes or "").strip() or None,
            adjustment_date=_date(adjustment_date, "Adjustment date",
                                  default=datetime.date.today()),
        )
    except (ValueError, warehouse_ops.OperationError) as exc:
        return _adjustment_form(context, rows=rows, values=values,
                                error=str(exc), status_code=400)
    return redirect(context, f"/operations/adjustments/{adjustment.id}?saved={adjustment.number}")


def _adjustment_detail(context, adjustment, *, error=None, saved="", status_code=200):
    return render(context, "adjustment_detail.html", status_code=status_code,
                  adjustment=adjustment, lines=list(adjustment.lines), error=error,
                  saved=saved, AdjustmentReason=AdjustmentReason,
                  net_value=warehouse_ops.adjustment_value(adjustment))


@router.get("/adjustments/{adjustment_id}")
def adjustment_detail(request: Request, adjustment_id: int, saved: str = ""):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockAdjustment.get_or_none(StockAdjustment.id == adjustment_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Adjustment not found", message="That adjustment no longer exists.")
    return _adjustment_detail(context, record, saved=(saved or "").strip())


@router.post("/adjustments/{adjustment_id}/approve")
def adjustment_approve(request: Request, adjustment_id: int,
                       csrf_token: str = Form("")):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockAdjustment.get_or_none(StockAdjustment.id == adjustment_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Adjustment not found", message="That adjustment no longer exists.")
    if not check_csrf(context, csrf_token):
        return _adjustment_detail(context, record, error="The form expired. Try again.",
                                  status_code=400)
    try:
        warehouse_ops.approve_adjustment(record, user=context.user)
    except auth.NotAuthorised:
        return forbidden(context, "approve stock adjustments")
    except (warehouse_ops.OperationError, inventory.StockError) as exc:
        return _adjustment_detail(context, record, error=str(exc), status_code=400)
    return redirect(context, f"/operations/adjustments/{record.id}?saved=Adjustment%20posted")


def _transfer_form(context, *, rows=None, values=None, error=None, status_code=200):
    values = values or {"from_warehouse_id": "", "to_warehouse_id": "",
                        "truck_hsrp": "", "notes": ""}
    return render(context, "transfer_form.html", status_code=status_code,
                  rows=rows or _line_rows(), values=values, items=_items(),
                  warehouses=_warehouses(), error=error)


@router.get("/transfers/new")
def transfer_new(request: Request):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_TRANSFER_STOCK):
        return forbidden(context, "create warehouse transfers")
    return _transfer_form(context)


@router.post("/transfers")
def transfer_create(
    request: Request, from_warehouse_id: str = Form(""),
    to_warehouse_id: str = Form(""), truck_hsrp: str = Form(""),
    notes: str = Form(""), item_id: list[str] = Form([]),
    quantity: list[str] = Form([]), csrf_token: str = Form(""),
):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_TRANSFER_STOCK):
        return forbidden(context, "create warehouse transfers")
    rows = _line_rows(item_id, quantity)
    values = {"from_warehouse_id": from_warehouse_id,
              "to_warehouse_id": to_warehouse_id,
              "truck_hsrp": truck_hsrp, "notes": notes}
    if not check_csrf(context, csrf_token):
        return _transfer_form(context, rows=rows, values=values,
                              error="The form expired. Try again.", status_code=400)
    try:
        transfer = warehouse_ops.create_transfer(
            _record(Warehouse, from_warehouse_id, "source warehouse"),
            _record(Warehouse, to_warehouse_id, "destination warehouse"),
            _operation_lines(rows), user=context.user,
            truck_hsrp=(truck_hsrp or "").strip() or None,
            notes=(notes or "").strip() or None,
        )
    except (ValueError, warehouse_ops.OperationError) as exc:
        return _transfer_form(context, rows=rows, values=values,
                              error=str(exc), status_code=400)
    return redirect(context, f"/operations/transfers/{transfer.id}?saved={transfer.number}")


def _transfer_detail(context, transfer, *, error=None, saved="", status_code=200):
    return render(context, "transfer_detail.html", status_code=status_code,
                  transfer=transfer, lines=list(transfer.lines), error=error,
                  saved=saved, TransferStatus=TransferStatus)


@router.get("/transfers/{transfer_id}")
def transfer_detail(request: Request, transfer_id: int, saved: str = ""):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockTransfer.get_or_none(StockTransfer.id == transfer_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Transfer not found", message="That transfer no longer exists.")
    return _transfer_detail(context, record, saved=(saved or "").strip())


@router.post("/transfers/{transfer_id}/dispatch")
def transfer_dispatch(request: Request, transfer_id: int,
                      dispatch_date: str = Form(""), truck_hsrp: str = Form(""),
                      csrf_token: str = Form("")):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockTransfer.get_or_none(StockTransfer.id == transfer_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Transfer not found", message="That transfer no longer exists.")
    if not check_csrf(context, csrf_token):
        return _transfer_detail(context, record, error="The form expired. Try again.",
                                status_code=400)
    try:
        warehouse_ops.dispatch_transfer(
            record, user=context.user,
            truck_hsrp=(truck_hsrp or "").strip() or None,
            dispatch_date=_date(dispatch_date, "Dispatch date",
                                default=datetime.date.today()),
        )
    except auth.NotAuthorised:
        return forbidden(context, "dispatch warehouse transfers")
    except (ValueError, warehouse_ops.OperationError, inventory.StockError) as exc:
        return _transfer_detail(context, record, error=str(exc), status_code=400)
    return redirect(context, f"/operations/transfers/{record.id}?saved=Transfer%20dispatched")


def _receive_transfer_page(context, transfer, *, error=None, status_code=200):
    return render(context, "receive_transfer.html", status_code=status_code,
                  transfer=transfer, lines=list(transfer.lines),
                  today=datetime.date.today().isoformat(), error=error)


@router.get("/transfers/{transfer_id}/receive")
def transfer_receive_page(request: Request, transfer_id: int):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockTransfer.get_or_none(StockTransfer.id == transfer_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Transfer not found", message="That transfer no longer exists.")
    if record.status != TransferStatus.IN_TRANSIT:
        return _transfer_detail(context, record,
                                error="Only an in-transit transfer can be received.",
                                status_code=400)
    return _receive_transfer_page(context, record)


@router.post("/transfers/{transfer_id}/receive")
def transfer_receive(
    request: Request, transfer_id: int, line_id: list[str] = Form([]),
    quantity: list[str] = Form([]), receive_date: str = Form(""),
    csrf_token: str = Form(""),
):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = StockTransfer.get_or_none(StockTransfer.id == transfer_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Transfer not found", message="That transfer no longer exists.")
    if not check_csrf(context, csrf_token):
        return _receive_transfer_page(context, record,
                                      error="The form expired. Try again.", status_code=400)
    try:
        entries = []
        for index, raw_id in enumerate(line_id):
            line = _record(StockTransferLine, raw_id, "transfer line")
            qty = _decimal(quantity[index] if index < len(quantity) else "",
                           f"Received quantity for {line.item.name}")
            entries.append({"transfer_line": line, "quantity": qty})
        warehouse_ops.receive_transfer(
            record, lines=entries, user=context.user,
            receive_date=_date(receive_date, "Receive date",
                               default=datetime.date.today()),
        )
    except auth.NotAuthorised:
        return forbidden(context, "receive warehouse transfers")
    except (ValueError, warehouse_ops.OperationError, inventory.StockError) as exc:
        return _receive_transfer_page(context, record, error=str(exc), status_code=400)
    return redirect(context, f"/operations/transfers/{record.id}?saved=Transfer%20received")


@router.post("/counts")
def count_create(request: Request, warehouse_id: str = Form(""),
                 notes: str = Form(""), csrf_token: str = Form("")):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_CYCLE_COUNT):
        return forbidden(context, "start cycle counts")
    if not check_csrf(context, csrf_token):
        return _operations_page(context, "counts", error="The form expired. Try again.",
                                status_code=400)
    try:
        count = warehouse_ops.open_cycle_count(
            _record(Warehouse, warehouse_id, "warehouse"), user=context.user,
            notes=(notes or "").strip() or None,
        )
    except (ValueError, warehouse_ops.OperationError) as exc:
        return _operations_page(context, "counts", error=str(exc), status_code=400)
    return redirect(context, f"/operations/counts/{count.id}?saved={count.number}")


def _count_detail(context, count, *, error=None, saved="", status_code=200):
    lines = list(count.lines)
    return render(context, "count_detail.html", status_code=status_code,
                  count=count, lines=lines, error=error, saved=saved,
                  CountStatus=CountStatus,
                  variance_count=sum(1 for line in lines
                                     if line.variance not in (None, Decimal("0"))))


@router.get("/counts/{count_id}")
def count_detail(request: Request, count_id: int, saved: str = ""):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = CycleCount.get_or_none(CycleCount.id == count_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Count not found", message="That cycle count no longer exists.")
    return _count_detail(context, record, saved=(saved or "").strip())


@router.post("/counts/{count_id}")
def count_save(
    request: Request, count_id: int, line_id: list[str] = Form([]),
    counted: list[str] = Form([]), approve: str = Form(""),
    csrf_token: str = Form(""),
):
    context, blocked = _context(request)
    if blocked is not None:
        return blocked
    record = CycleCount.get_or_none(CycleCount.id == count_id)
    if record is None:
        return render(context, "error.html", status_code=404,
                      title="Count not found", message="That cycle count no longer exists.")
    if not check_csrf(context, csrf_token):
        return _count_detail(context, record, error="The form expired. Try again.",
                             status_code=400)
    try:
        values = {}
        for index, raw_id in enumerate(line_id):
            raw_value = counted[index] if index < len(counted) else ""
            if not raw_value.strip():
                continue
            line = _record(CycleCountLine, raw_id, "count line")
            if line.count_id != record.id:
                raise ValueError("A count line does not belong to this cycle count.")
            values[line.id] = _decimal(raw_value, f"Count for {line.item.name}")
        warehouse_ops.record_counts(record, values, user=context.user)
        if approve:
            warehouse_ops.approve_cycle_count(record, user=context.user)
    except auth.NotAuthorised:
        return forbidden(context, "approve cycle-count variances" if approve
                         else "record cycle counts")
    except (ValueError, warehouse_ops.OperationError, inventory.StockError) as exc:
        return _count_detail(context, record, error=str(exc), status_code=400)
    message = "Count%20approved" if approve else "Counts%20saved"
    return redirect(context, f"/operations/counts/{record.id}?saved={message}")
