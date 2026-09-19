"""Purchase orders: creation, totals and the reorder logic behind Order / Order Max.

Goods receipts (which move stock) are built on top of this in Phase 2.
"""
import datetime
from decimal import Decimal

from database.connection import db, is_postgres
from database.models import (
    ZERO,
    Direction,
    DocType,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseStatus,
    Serial,
    SerialStatus,
    Supplier,
    SupplierReturn,
    SupplierReturnLine,
    Warehouse,
)
from services import auth, gst, inventory, numbering


class PurchasingError(Exception):
    """A purchasing operation was refused. Safe to show the user."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def lock_line(line_id) -> PurchaseOrderLine:
    """Re-read an order line inside the transaction, locking it on PostgreSQL.

    Two warehouse PCs each hold a copy of the order from when their screen opened.
    Validating or incrementing against that stale copy lets the second receipt both
    exceed the ordered quantity and overwrite the first one's progress, so every
    quantity decision has to be made against a freshly read, locked row.
    """
    query = PurchaseOrderLine.select().where(PurchaseOrderLine.id == line_id)
    if is_postgres():
        query = query.for_update()
    line = query.first()
    if line is None:
        raise PurchasingError("That order line no longer exists — reload the order.")
    return line


def recalculate_totals(order: PurchaseOrder) -> PurchaseOrder:
    """Recompute line totals, tax and the order total from its lines."""
    subtotal = ZERO
    tax_total = ZERO
    interstate = gst.is_interstate(order.warehouse.state_code, order.supplier.state_code)

    for line in order.lines:
        line_total = (_dec(line.quantity) * _dec(line.unit_cost)).quantize(Decimal("0.01"))
        line.line_total = line_total
        line.save()
        subtotal += line_total
        cgst, sgst, igst = gst.split_tax(line_total, line.gst_rate, interstate)
        tax_total += cgst + sgst + igst

    order.subtotal = subtotal
    order.tax_total = tax_total
    order.total = subtotal + tax_total
    order.save()
    return order


def create_purchase_order(supplier, warehouse, lines, user=None, expected_date=None,
                          notes=None, status=PurchaseStatus.DRAFT) -> PurchaseOrder:
    """Create a purchase order.

    `lines` is an iterable of dicts: {item, quantity, unit_cost (optional),
    gst_rate (optional), notes (optional)}.
    """
    auth.require(user, auth.PERM_CREATE_PURCHASE, "raise a purchase order")
    lines = [line for line in lines if _dec(line.get("quantity")) > 0]
    if not lines:
        raise PurchasingError("A purchase order needs at least one line with a quantity.")
    if supplier is None:
        raise PurchasingError("Choose a supplier for the purchase order.")
    if warehouse is None:
        raise PurchasingError("Choose the warehouse the goods will be delivered to.")

    if expected_date is None:
        lead = supplier.lead_time_days or 0
        expected_date = datetime.date.today() + datetime.timedelta(days=lead)

    with db.atomic():
        order = PurchaseOrder.create(
            number=numbering.next_number("PURCHASE_ORDER"),
            supplier=supplier,
            warehouse=warehouse,
            status=status,
            order_date=datetime.date.today(),
            expected_date=expected_date,
            notes=notes,
            created_by=user,
        )
        for line in lines:
            item = line["item"]
            unit_cost = line.get("unit_cost")
            if unit_cost in (None, ""):
                unit_cost = item.last_purchase_cost or item.avg_cost or ZERO
            PurchaseOrderLine.create(
                order=order,
                item=item,
                quantity=_dec(line["quantity"]),
                unit_cost=_dec(unit_cost),
                gst_rate=_dec(line.get("gst_rate", item.gst_rate)),
                hsn_code=line.get("hsn_code", item.hsn_code),
                notes=line.get("notes"),
            )
        recalculate_totals(order)

    auth.record_audit(
        user, "CREATE", "PurchaseOrder", order.id,
        f"Created {order.number} for {supplier.name} ({len(lines)} lines)",
    )
    return order


def build_reorder_suggestions(stock_levels, to_maximum=True):
    """Turn stock levels into proposed order lines.

    Returns a list of dicts: {item, warehouse, suggested, on_hand, minimum, maximum,
    supplier, unit_cost}. Levels that need nothing are skipped.
    """
    suggestions = []
    for level in stock_levels:
        quantity = inventory.suggested_order_quantity(level, to_maximum=to_maximum)
        if quantity <= 0:
            continue
        item = level.item
        suggestions.append({
            "item": item,
            "warehouse": level.warehouse,
            "suggested": quantity,
            "on_hand": _dec(level.on_hand),
            "minimum": _dec(level.effective_min),
            "maximum": _dec(level.effective_max),
            "supplier": item.preferred_supplier,
            "unit_cost": _dec(item.last_purchase_cost or item.avg_cost),
        })
    return suggestions


def create_orders_from_requests(requests, user=None, status=PurchaseStatus.DRAFT):
    """Create draft purchase orders from reorder requests, grouped by supplier+warehouse.

    `requests` is an iterable of dicts: {item, warehouse, quantity, unit_cost, supplier}.
    Returns (orders, unassigned_items) — items with no supplier can't be ordered and are
    reported back rather than silently dropped.
    """
    grouped = {}
    unassigned = []

    for request in requests:
        quantity = _dec(request.get("quantity"))
        if quantity <= 0:
            continue
        item = request["item"]
        supplier = request.get("supplier") or item.preferred_supplier
        warehouse = request["warehouse"]
        if supplier is None:
            unassigned.append(item)
            continue
        grouped.setdefault((supplier.id, warehouse.id), (supplier, warehouse, []))
        grouped[(supplier.id, warehouse.id)][2].append({
            "item": item,
            "quantity": quantity,
            "unit_cost": request.get("unit_cost"),
            "gst_rate": item.gst_rate,
            "hsn_code": item.hsn_code,
        })

    orders = []
    for supplier, warehouse, lines in grouped.values():
        orders.append(create_purchase_order(
            supplier, warehouse, lines, user=user, status=status,
            notes="Generated from Inventory reorder",
        ))
    return orders, unassigned


# --- Order lifecycle ----------------------------------------------------------------

def confirm_order(order: PurchaseOrder, user=None) -> PurchaseOrder:
    """Draft → Ordered. The point at which the order is considered sent to the supplier."""
    auth.require(user, auth.PERM_CREATE_PURCHASE, "send a purchase order")
    if order.status != PurchaseStatus.DRAFT:
        raise PurchasingError(
            f"{order.number} is already "
            f"{PurchaseStatus.LABELS.get(order.status, order.status)}."
        )
    if not order.lines.count():
        raise PurchasingError(f"{order.number} has no lines to order.")

    order.status = PurchaseStatus.ORDERED
    order.save()
    auth.record_audit(user, "CONFIRM", "PurchaseOrder", order.id,
                      f"Confirmed {order.number} to {order.supplier.name}")
    return order


def cancel_order(order: PurchaseOrder, user=None, reason=None) -> PurchaseOrder:
    """Cancel an order. Anything already received stays received."""
    auth.require(user, auth.PERM_CREATE_PURCHASE, "cancel a purchase order")
    if order.status in (PurchaseStatus.RECEIVED, PurchaseStatus.CANCELLED):
        raise PurchasingError(
            f"{order.number} is {PurchaseStatus.LABELS.get(order.status, order.status)} "
            "and cannot be cancelled."
        )
    order.status = PurchaseStatus.CANCELLED
    if reason:
        order.notes = f"{order.notes}\n[Cancelled] {reason}" if order.notes \
            else f"[Cancelled] {reason}"
    order.save()
    auth.record_audit(user, "CANCEL", "PurchaseOrder", order.id,
                      f"Cancelled {order.number}" + (f": {reason}" if reason else ""))
    return order


def _refresh_status(order: PurchaseOrder):
    """Derive the order status from how much of it has actually arrived."""
    if order.status == PurchaseStatus.CANCELLED:
        return order

    lines = list(order.lines)
    if not lines:
        return order

    any_received = any(_dec(line.received_quantity) > 0 for line in lines)
    all_complete = all(line.outstanding <= 0 for line in lines)

    if all_complete:
        order.status = PurchaseStatus.RECEIVED
        order.received_date = datetime.date.today()
    elif any_received:
        order.status = PurchaseStatus.PARTIAL
    order.save()
    return order


# --- Goods receipt ------------------------------------------------------------------

def receive_goods(order, lines, user=None, receipt_date=None, truck_hsrp=None,
                  supplier_invoice_no=None, notes=None) -> GoodsReceipt:
    """Record what actually arrived, move it into stock, and reprice the item.

    `lines` is an iterable of dicts:
        {order_line, quantity, unit_cost (optional), rejected_quantity (optional),
         lot_number (optional), expiry_date (optional), serials (optional list)}

    Quantities may be short — the order simply stays partially received. Over-receipt is
    refused, because it is nearly always a typo rather than a genuine surplus.
    """
    auth.require(user, auth.PERM_RECEIVE_GOODS, "receive goods")
    if order.status == PurchaseStatus.CANCELLED:
        raise PurchasingError(f"{order.number} is cancelled and cannot be received.")
    if order.status == PurchaseStatus.RECEIVED:
        raise PurchasingError(f"{order.number} has already been fully received.")

    prepared = []
    for entry in lines:
        quantity = _dec(entry.get("quantity"))
        if quantity <= 0:
            continue
        order_line = entry["order_line"]
        if order_line.order_id != order.id:
            raise PurchasingError("A receipt line does not belong to this order.")

        # A friendly early check; the authoritative one runs against a locked row below.
        item = order_line.item
        serials = [s for s in (entry.get("serials") or []) if str(s).strip()]
        if item.is_serial_tracked and len(serials) != int(quantity):
            raise PurchasingError(
                f"{item.name} is serial tracked: {int(quantity)} serial number(s) "
                f"required, {len(serials)} supplied."
            )
        if item.is_lot_tracked and not (entry.get("lot_number") or "").strip():
            raise PurchasingError(
                f"{item.name} is lot tracked — enter the batch number that arrived."
            )

        prepared.append({
            "order_line": order_line,
            "item": item,
            "quantity": quantity,
            "unit_cost": _dec(entry.get("unit_cost", order_line.unit_cost)),
            "rejected": _dec(entry.get("rejected_quantity")),
            "lot_number": (entry.get("lot_number") or "").strip() or None,
            "expiry_date": entry.get("expiry_date"),
            "serials": serials,
        })

    if not prepared:
        raise PurchasingError("Enter a received quantity for at least one line.")

    with db.atomic():
        # Re-read every line under a lock and validate against what is actually
        # outstanding right now, not against what this PC last saw.
        for entry in prepared:
            locked = lock_line(entry["order_line"].id)
            outstanding = locked.outstanding
            if entry["quantity"] > outstanding:
                raise PurchasingError(
                    f"{locked.item.name}: receiving "
                    f"{inventory.fmt_qty(entry['quantity'])} but only "
                    f"{inventory.fmt_qty(outstanding)} is outstanding on "
                    f"{order.number}. Someone else may have received against this "
                    f"order — reload it and try again."
                )
            entry["order_line"] = locked

        receipt = GoodsReceipt.create(
            number=numbering.next_number("GOODS_RECEIPT"),
            order=order,
            supplier=order.supplier,
            warehouse=order.warehouse,
            receipt_date=receipt_date or datetime.date.today(),
            truck_hsrp=truck_hsrp,
            received_by=user,
            notes=notes,
        )

        for entry in prepared:
            item = entry["item"]
            quantity = entry["quantity"]
            unit_cost = entry["unit_cost"]

            lot = None
            if item.is_lot_tracked:
                lot = inventory.get_or_create_lot(
                    item, entry["lot_number"], expiry_date=entry["expiry_date"],
                    supplier=order.supplier, unit_cost=unit_cost,
                    received_date=receipt.receipt_date,
                )

            # Reprice before the movement: the average blends against the quantity that
            # was on hand *before* this delivery landed.
            inventory.update_moving_average(item, quantity, unit_cost)

            inventory.apply_movement(
                item, order.warehouse, Direction.IN, quantity, DocType.GOODS_RECEIPT,
                doc_number=receipt.number, doc_id=receipt.id, unit_cost=unit_cost,
                lot=lot, user=user, employee=user.employee if user else None,
                supplier=order.supplier, truck_hsrp=truck_hsrp,
                notes=f"Against {order.number}",
            )

            if item.is_serial_tracked and entry["serials"]:
                inventory.receive_serials(item, order.warehouse, entry["serials"],
                                          lot=lot, unit_cost=unit_cost)

            GoodsReceiptLine.create(
                receipt=receipt, order_line=entry["order_line"], item=item,
                quantity=quantity, unit_cost=unit_cost, lot=lot,
                rejected_quantity=entry["rejected"],
            )

            order_line = entry["order_line"]
            order_line.received_quantity = _dec(order_line.received_quantity) + quantity
            # A price change on delivery is the real cost — carry it back to the order.
            if unit_cost != _dec(order_line.unit_cost):
                order_line.unit_cost = unit_cost
            order_line.save()

        if supplier_invoice_no:
            order.supplier_invoice_no = supplier_invoice_no
        recalculate_totals(order)
        _refresh_status(order)

    auth.record_audit(
        user, "RECEIVE", "GoodsReceipt", receipt.id,
        f"{receipt.number}: received {len(prepared)} line(s) against {order.number}",
    )
    return receipt


def outstanding_lines(order):
    """Lines still awaiting delivery, for the receive dialog."""
    return [line for line in order.lines if line.outstanding > 0]


# --- Supplier returns ---------------------------------------------------------------

def create_supplier_return(supplier, warehouse, lines, user=None, order=None,
                           reason=None, notes=None) -> SupplierReturn:
    """Send goods back to a supplier, removing them from stock.

    A linked order constrains each item to its received quantity minus earlier returns.
    Serialized units must currently be in stock at the selected warehouse and are marked
    RETURNED after their stock movement posts.
    """
    auth.require(user, auth.PERM_RECEIVE_GOODS, "return goods to a supplier")
    if supplier is None or warehouse is None:
        raise PurchasingError("Choose the supplier and warehouse for this return.")
    if order is not None and (order.supplier_id != supplier.id
                              or order.warehouse_id != warehouse.id):
        raise PurchasingError(
            "The linked purchase order must belong to the selected supplier and warehouse.")

    prepared = []
    requested = {}
    serial_keys = set()
    for line in lines:
        quantity = _dec(line.get("quantity"))
        if not quantity.is_finite() or quantity <= 0:
            continue
        item = line.get("item")
        if item is None:
            raise PurchasingError("Every supplier return line needs an item.")
        serials = [str(value).strip() for value in (line.get("serials") or [])
                   if str(value).strip()]
        if len(serials) != len(set(serials)):
            raise PurchasingError(f"{item.name}: each serial number can appear only once.")
        if item.is_serial_tracked:
            if quantity != quantity.to_integral_value() or len(serials) != int(quantity):
                raise PurchasingError(
                    f"{item.name}: enter one serial number for each returned unit.")
            serial_records = []
            for number in serials:
                key = (item.id, number)
                if key in serial_keys:
                    raise PurchasingError(f"Serial {number} appears on more than one line.")
                serial_keys.add(key)
                serial = Serial.get_or_none(
                    (Serial.item == item) & (Serial.serial_number == number))
                if (serial is None or serial.status != SerialStatus.IN_STOCK
                        or serial.warehouse_id != warehouse.id):
                    raise PurchasingError(
                        f"Serial {number} is not in stock at {warehouse.name} for {item.name}.")
                serial_records.append(serial)
        else:
            if serials:
                raise PurchasingError(f"{item.name} is not configured for serial tracking.")
            serial_records = []
        entry = dict(line)
        entry.update({"item": item, "quantity": quantity, "serials": serials,
                      "serial_records": serial_records})
        prepared.append(entry)
        requested[item.id] = requested.get(item.id, ZERO) + quantity
    if not prepared:
        raise PurchasingError("A supplier return needs at least one line.")

    if order is not None:
        received = {}
        for order_line in order.lines:
            received[order_line.item_id] = (
                received.get(order_line.item_id, ZERO)
                + _dec(order_line.received_quantity))
        previous = {}
        prior_lines = (SupplierReturnLine.select(SupplierReturnLine)
                       .join(SupplierReturn)
                       .where(SupplierReturn.order == order))
        for line in prior_lines:
            previous[line.item_id] = previous.get(line.item_id, ZERO) + _dec(line.quantity)
        for item_id, quantity in requested.items():
            remaining = received.get(item_id, ZERO) - previous.get(item_id, ZERO)
            if quantity > remaining:
                item = next(entry["item"] for entry in prepared
                            if entry["item"].id == item_id)
                raise PurchasingError(
                    f"{item.name}: only {inventory.fmt_qty(max(ZERO, remaining))} "
                    "received unit(s) remain eligible for return on this order.")

    with db.atomic():
        record = SupplierReturn.create(
            number=numbering.next_number("SUPPLIER_RETURN"),
            supplier=supplier, warehouse=warehouse, order=order,
            return_date=datetime.date.today(), reason=reason, notes=notes,
            created_by=user,
        )
        total = ZERO
        for entry in prepared:
            item = entry["item"]
            quantity = entry["quantity"]
            unit_cost = _dec(entry.get("unit_cost", item.avg_cost))
            explicit_lot = entry.get("lot")

            # Split lot-tracked returns across available stock in expiry order.
            if item.is_lot_tracked and explicit_lot is None:
                allocation = inventory.allocate_fefo(item, warehouse, quantity)
            else:
                allocation = [(explicit_lot, quantity)]

            for lot, lot_quantity in allocation:
                inventory.apply_movement(
                    item, warehouse, Direction.OUT, lot_quantity, DocType.SUPPLIER_RETURN,
                    doc_number=record.number, doc_id=record.id, unit_cost=unit_cost,
                    lot=lot, user=user, supplier=supplier,
                    notes=reason or "Returned to supplier",
                )
                SupplierReturnLine.create(
                    supplier_return=record, item=item, quantity=lot_quantity,
                    unit_cost=unit_cost, lot=lot,
                )
            if entry["serials"]:
                returned_serials = inventory.ship_serials(item, entry["serials"])
                for serial in returned_serials:
                    serial.status = SerialStatus.RETURNED
                    serial.warehouse = None
                    serial.save()
            total += quantity * unit_cost

        record.total = total
        record.save()

    auth.record_audit(
        user, "CREATE", "SupplierReturn", record.id,
        f"{record.number}: returned {len(prepared)} line(s) to {supplier.name}",
    )
    return record

def open_orders(warehouse=None, supplier=None):
    query = PurchaseOrder.select().where(PurchaseOrder.status.in_(PurchaseStatus.OPEN))
    if warehouse is not None:
        query = query.where(PurchaseOrder.warehouse == warehouse)
    if supplier is not None:
        query = query.where(PurchaseOrder.supplier == supplier)
    return list(query.order_by(PurchaseOrder.expected_date.asc()))


def overdue_orders(warehouse=None):
    """Open orders whose ETA has passed — the Delays widget and PO reminders.

    Supplier and warehouse are joined because callers read both names; without the join
    each row would cost two extra round trips.
    """
    query = (PurchaseOrder.select(PurchaseOrder, Supplier, Warehouse)
             .join(Supplier).switch(PurchaseOrder).join(Warehouse)
             .where(
                 (PurchaseOrder.status.in_(PurchaseStatus.OPEN))
                 & (PurchaseOrder.expected_date.is_null(False))
                 & (PurchaseOrder.expected_date < datetime.date.today())))
    if warehouse is not None:
        query = query.where(PurchaseOrder.warehouse == warehouse)
    return list(query.order_by(PurchaseOrder.expected_date.asc()))


def on_order_quantity(item, warehouse=None) -> Decimal:
    """Quantity already on open purchase orders — stops double-ordering."""
    query = (
        PurchaseOrderLine.select(PurchaseOrderLine, PurchaseOrder)
        .join(PurchaseOrder)
        .where(
            (PurchaseOrderLine.item == item)
            & (PurchaseOrder.status.in_(PurchaseStatus.OPEN))
        )
    )
    if warehouse is not None:
        query = query.where(PurchaseOrder.warehouse == warehouse)
    return sum((line.outstanding for line in query), ZERO)


def delete_order(order: PurchaseOrder, user=None):
    """Delete a draft order. Anything already received is kept for the audit trail."""
    auth.require(user, auth.PERM_DELETE_RECORDS, "delete records")
    if order.status != PurchaseStatus.DRAFT:
        raise PurchasingError(
            f"{order.number} is {PurchaseStatus.LABELS.get(order.status, order.status)} "
            "and can no longer be deleted. Cancel it instead."
        )
    number = order.number
    with db.atomic():
        order.delete_instance(recursive=True)
    auth.record_audit(user, "DELETE", "PurchaseOrder", None, f"Deleted draft {number}")
