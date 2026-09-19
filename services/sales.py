"""Sales orders through fulfilment, GST invoicing and payment collection.

The sequence is: create (draft) → confirm (reserves stock) → fulfil (moves stock out and
freezes the cost of goods sold) → invoice (splits GST) → payments (partial allowed).

Cost of goods sold is captured on the fulfilment line at ship time and never
recalculated, so a later price change cannot rewrite last quarter's profit.
"""
import datetime
from decimal import Decimal

from database.connection import db, is_postgres
from database.models import (
    ZERO,
    CustomerReturn,
    CustomerReturnLine,
    Direction,
    DocType,
    Fulfillment,
    FulfillmentLine,
    Invoice,
    Payment,
    PaymentStatus,
    SalesOrder,
    Serial,
    SerialStatus,
    SalesOrderLine,
    SalesStatus,
)
from services import auth, gst, inventory, numbering, pricing

TWO_PLACES = Decimal("0.01")


class SalesError(Exception):
    """A sales operation was refused. Safe to show the user."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def lock_line(line_id) -> SalesOrderLine:
    """Re-read an order line inside the transaction, locking it on PostgreSQL.

    Same reasoning as the purchasing side: two PCs holding stale copies of the order
    would otherwise each ship the full quantity, double-shipping the customer and
    losing the first shipment's progress off the line.
    """
    query = SalesOrderLine.select().where(SalesOrderLine.id == line_id)
    if is_postgres():
        query = query.for_update()
    line = query.first()
    if line is None:
        raise SalesError("That order line no longer exists — reload the order.")
    return line


# --- Totals -------------------------------------------------------------------------

def recalculate_totals(order: SalesOrder) -> SalesOrder:
    """Recompute line totals, GST and the order total from its lines."""
    subtotal = ZERO
    tax_total = ZERO
    interstate = gst.is_interstate(order.warehouse.state_code, order.customer.state_code)

    for line in order.lines:
        gross = _dec(line.quantity) * _dec(line.unit_price)
        discount = gross * _dec(line.discount_percent) / Decimal("100")
        line_total = (gross - discount).quantize(TWO_PLACES)
        line.line_total = line_total
        line.save()
        subtotal += line_total
        cgst, sgst, igst = gst.split_tax(line_total, line.gst_rate, interstate)
        tax_total += cgst + sgst + igst

    order.subtotal = subtotal
    order.tax_total = tax_total
    order.total = subtotal + tax_total
    _refresh_payment_status(order, save=False)
    order.save()
    return order


def _refresh_payment_status(order: SalesOrder, save=True):
    paid = _dec(order.amount_paid)
    total = _dec(order.total)
    if paid <= 0:
        order.payment_status = PaymentStatus.UNPAID
    elif paid + Decimal("0.01") >= total:
        order.payment_status = PaymentStatus.PAID
    else:
        order.payment_status = PaymentStatus.PARTIAL
    if save:
        order.save()
    return order


# --- Order lifecycle ----------------------------------------------------------------

def create_sales_order(customer, warehouse, lines, user=None, promised_date=None,
                       truck_hsrp=None, delivery_address=None, delivery_pincode=None,
                       notes=None, status=SalesStatus.DRAFT) -> SalesOrder:
    """Create a sales order.

    `lines`: [{item, quantity, unit_price (optional — resolved from the customer's price
    list when omitted), discount_percent (optional), notes (optional)}]
    """
    auth.require(user, auth.PERM_CREATE_SALES, "raise a sales order")
    lines = [line for line in lines if _dec(line.get("quantity")) > 0]
    if not lines:
        raise SalesError("A sales order needs at least one line with a quantity.")
    if customer is None:
        raise SalesError("Choose a customer for the sales order.")
    if warehouse is None:
        raise SalesError("Choose the warehouse the goods will ship from.")

    with db.atomic():
        order = SalesOrder.create(
            number=numbering.next_number("SALES_ORDER"),
            customer=customer,
            warehouse=warehouse,
            status=SalesStatus.DRAFT,
            order_date=datetime.date.today(),
            promised_date=promised_date,
            delivery_address=delivery_address or customer.delivery_address,
            delivery_pincode=delivery_pincode or customer.delivery_pincode,
            truck_hsrp=truck_hsrp,
            notes=notes,
            created_by=user,
        )
        for line in lines:
            item = line["item"]
            quantity = _dec(line["quantity"])
            unit_price = line.get("unit_price")
            if unit_price in (None, ""):
                unit_price = pricing.resolve_price(item, customer, quantity)
            SalesOrderLine.create(
                order=order,
                item=item,
                quantity=quantity,
                unit_price=_dec(unit_price),
                discount_percent=_dec(line.get("discount_percent")),
                gst_rate=_dec(line.get("gst_rate", item.gst_rate)),
                hsn_code=line.get("hsn_code", item.hsn_code),
                notes=line.get("notes"),
            )
        recalculate_totals(order)

        if status == SalesStatus.CONFIRMED:
            confirm_order(order, user=user)

    auth.record_audit(user, "CREATE", "SalesOrder", order.id,
                      f"Created {order.number} for {customer.name} ({len(lines)} lines)")
    return order


def shortfalls(order: SalesOrder):
    """Lines the warehouse cannot currently cover, as [(line, available), ...].

    Reported rather than blocked: taking an order for goods not yet in stock is normal —
    you then raise a purchase order for them.
    """
    result = []
    for line in order.lines:
        free = inventory.available(line.item, order.warehouse)
        if line.outstanding > free:
            result.append((line, free))
    return result


def confirm_order(order: SalesOrder, user=None) -> SalesOrder:
    """Draft → Confirmed, reserving stock against each line."""
    auth.require(user, auth.PERM_CREATE_SALES, "confirm a sales order")
    if order.status != SalesStatus.DRAFT:
        raise SalesError(
            f"{order.number} is already "
            f"{SalesStatus.LABELS.get(order.status, order.status)}."
        )
    if not order.lines.count():
        raise SalesError(f"{order.number} has no lines to confirm.")

    with db.atomic():
        for line in order.lines:
            # enforce=False: a confirmed order may legitimately exceed current stock.
            inventory.reserve(line.item, order.warehouse, line.outstanding, enforce=False)
        order.status = SalesStatus.CONFIRMED
        order.save()

    auth.record_audit(user, "CONFIRM", "SalesOrder", order.id,
                      f"Confirmed {order.number} for {order.customer.name}")
    return order


def cancel_order(order: SalesOrder, user=None, reason=None) -> SalesOrder:
    """Cancel an order, releasing any stock still reserved for it."""
    auth.require(user, auth.PERM_CREATE_SALES, "cancel a sales order")
    if order.status in (SalesStatus.SHIPPED, SalesStatus.CANCELLED):
        raise SalesError(
            f"{order.number} is {SalesStatus.LABELS.get(order.status, order.status)} "
            "and cannot be cancelled."
        )

    with db.atomic():
        if order.status in (SalesStatus.CONFIRMED, SalesStatus.PARTIAL):
            for line in order.lines:
                inventory.release_reservation(line.item, order.warehouse, line.outstanding)
        order.status = SalesStatus.CANCELLED
        if reason:
            order.notes = f"{order.notes}\n[Cancelled] {reason}" if order.notes \
                else f"[Cancelled] {reason}"
        order.save()

    auth.record_audit(user, "CANCEL", "SalesOrder", order.id,
                      f"Cancelled {order.number}" + (f": {reason}" if reason else ""))
    return order


def _refresh_status(order: SalesOrder):
    if order.status == SalesStatus.CANCELLED:
        return order
    lines = list(order.lines)
    if not lines:
        return order

    any_shipped = any(_dec(line.shipped_quantity) > 0 for line in lines)
    all_shipped = all(line.outstanding <= 0 for line in lines)

    if all_shipped:
        order.status = SalesStatus.SHIPPED
        order.shipped_date = datetime.date.today()
    elif any_shipped:
        order.status = SalesStatus.PARTIAL
    order.save()
    return order


# --- Fulfilment ---------------------------------------------------------------------

def fulfill(order, lines, user=None, ship_date=None, truck_hsrp=None,
            notes=None) -> Fulfillment:
    """Ship goods against an order: move stock out and freeze the cost of goods sold.

    `lines`: [{order_line, quantity, lot (optional — FEFO otherwise),
               serials (optional list)}]

    Short shipments are fine; the order stays partially shipped.
    """
    auth.require(user, auth.PERM_FULFILL_GOODS, "ship goods")
    if order.status == SalesStatus.CANCELLED:
        raise SalesError(f"{order.number} is cancelled and cannot be shipped.")
    if order.status == SalesStatus.DRAFT:
        raise SalesError(f"Confirm {order.number} before shipping it.")
    if order.status == SalesStatus.SHIPPED:
        raise SalesError(f"{order.number} has already shipped in full.")

    prepared = []
    for entry in lines:
        quantity = _dec(entry.get("quantity"))
        if quantity <= 0:
            continue
        order_line = entry["order_line"]
        if order_line.order_id != order.id:
            raise SalesError("A fulfilment line does not belong to this order.")

        # A friendly early check; the authoritative one runs against a locked row below.
        item = order_line.item
        serials = [s for s in (entry.get("serials") or []) if str(s).strip()]
        if item.is_serial_tracked and len(serials) != int(quantity):
            raise SalesError(
                f"{item.name} is serial tracked: {int(quantity)} serial number(s) "
                f"required, {len(serials)} supplied."
            )

        prepared.append({
            "order_line": order_line, "item": item, "quantity": quantity,
            "lot": entry.get("lot"), "serials": serials,
        })

    if not prepared:
        raise SalesError("Enter a quantity to ship on at least one line.")

    with db.atomic():
        # Validate against a freshly locked row, not this PC's stale copy.
        for entry in prepared:
            locked = lock_line(entry["order_line"].id)
            outstanding = locked.outstanding
            if entry["quantity"] > outstanding:
                raise SalesError(
                    f"{locked.item.name}: shipping "
                    f"{inventory.fmt_qty(entry['quantity'])} but only "
                    f"{inventory.fmt_qty(outstanding)} is outstanding on "
                    f"{order.number}. Someone else may have shipped against this "
                    f"order — reload it and try again."
                )
            entry["order_line"] = locked

        fulfillment = Fulfillment.create(
            number=numbering.next_number("FULFILLMENT"),
            order=order,
            warehouse=order.warehouse,
            ship_date=ship_date or datetime.date.today(),
            truck_hsrp=truck_hsrp or order.truck_hsrp,
            shipped_by=user,
            notes=notes,
        )

        for entry in prepared:
            item = entry["item"]
            quantity = entry["quantity"]
            order_line = entry["order_line"]
            # Freeze COGS now. Later receipts will move avg_cost; this must not follow.
            unit_cost = _dec(item.avg_cost)

            if item.is_lot_tracked:
                allocation = ([(entry["lot"], quantity)] if entry["lot"] is not None
                              else inventory.allocate_fefo(item, order.warehouse, quantity))
            else:
                allocation = [(None, quantity)]

            for lot, lot_quantity in allocation:
                inventory.apply_movement(
                    item, order.warehouse, Direction.OUT, lot_quantity,
                    DocType.FULFILLMENT, doc_number=fulfillment.number,
                    doc_id=fulfillment.id, unit_cost=unit_cost, lot=lot, user=user,
                    employee=user.employee if user else None, customer=order.customer,
                    truck_hsrp=fulfillment.truck_hsrp,
                    notes=f"Against {order.number}",
                )
                FulfillmentLine.create(
                    fulfillment=fulfillment, order_line=order_line, item=item,
                    quantity=lot_quantity, unit_price=_dec(order_line.unit_price),
                    unit_cost=unit_cost, lot=lot,
                )

            if item.is_serial_tracked and entry["serials"]:
                inventory.ship_serials(item, entry["serials"])

            inventory.release_reservation(item, order.warehouse, quantity)
            order_line.shipped_quantity = _dec(order_line.shipped_quantity) + quantity
            order_line.save()

        if truck_hsrp:
            order.truck_hsrp = truck_hsrp
        _refresh_status(order)

    auth.record_audit(
        user, "FULFILL", "Fulfillment", fulfillment.id,
        f"{fulfillment.number}: shipped {len(prepared)} line(s) of {order.number}",
    )
    return fulfillment


def outstanding_lines(order):
    """Lines still to ship, for the fulfilment dialog."""
    return [line for line in order.lines if line.outstanding > 0]


# --- Invoicing ----------------------------------------------------------------------

def create_invoice(order: SalesOrder, user=None, invoice_date=None) -> Invoice:
    """Raise a GST invoice for the order.

    Tax is CGST+SGST when the customer is in the warehouse's state, IGST otherwise, and
    the total is rounded to the nearest rupee with the adjustment recorded.
    """
    auth.require(user, auth.PERM_CREATE_SALES, "raise an invoice")
    if order.status in (SalesStatus.DRAFT, SalesStatus.CANCELLED):
        raise SalesError(
            f"{order.number} is {SalesStatus.LABELS.get(order.status, order.status)} — "
            "confirm it before invoicing."
        )

    interstate = gst.is_interstate(order.warehouse.state_code, order.customer.state_code)
    subtotal = cgst_total = sgst_total = igst_total = ZERO

    for line in order.lines:
        line_total = _dec(line.line_total)
        subtotal += line_total
        cgst, sgst, igst = gst.split_tax(line_total, line.gst_rate, interstate)
        cgst_total += cgst
        sgst_total += sgst
        igst_total += igst

    gross = subtotal + cgst_total + sgst_total + igst_total
    rounded, adjustment = gst.round_off(gross)

    due_date = None
    if order.customer.payment_terms_days:
        due_date = (invoice_date or datetime.date.today()) + datetime.timedelta(
            days=order.customer.payment_terms_days
        )

    with db.atomic():
        invoice = Invoice.create(
            number=numbering.next_number("INVOICE"),
            order=order,
            customer=order.customer,
            invoice_date=invoice_date or datetime.date.today(),
            due_date=due_date,
            place_of_supply=gst.state_name(order.customer.state_code) or None,
            is_interstate=interstate,
            subtotal=subtotal,
            cgst=cgst_total,
            sgst=sgst_total,
            igst=igst_total,
            round_off=adjustment,
            total=rounded,
            created_by=user,
        )

    auth.record_audit(user, "CREATE", "Invoice", invoice.id,
                      f"{invoice.number} for {order.number} — {rounded}")
    return invoice


def invoice_for(order: SalesOrder):
    return Invoice.select().where(Invoice.order == order).order_by(Invoice.id.desc()).first()


# --- Payments -----------------------------------------------------------------------

def record_payment(order, amount, user=None, method="CASH", reference=None,
                   payment_date=None, notes=None, allow_overpayment=False) -> Payment:
    """Record a full or part payment against an order."""
    auth.require(user, auth.PERM_RECORD_PAYMENT, "record a payment")
    if order.status == SalesStatus.CANCELLED:
        raise SalesError(f"{order.number} is cancelled and cannot accept payments.")
    amount = _dec(amount)
    if amount <= 0:
        raise SalesError("Payment amount must be greater than zero.")

    balance = _dec(order.total) - _dec(order.amount_paid)
    if amount > balance and not allow_overpayment:
        raise SalesError(
            f"{order.number} has {balance} outstanding — that payment of {amount} "
            f"exceeds it. Tick 'allow overpayment' if this is intentional."
        )

    with db.atomic():
        payment = Payment.create(
            number=numbering.next_number("PAYMENT"),
            order=order,
            customer=order.customer,
            amount=amount,
            payment_date=payment_date or datetime.date.today(),
            method=method,
            reference=reference,
            notes=notes,
            recorded_by=user,
        )
        order.amount_paid = _dec(order.amount_paid) + amount
        _refresh_payment_status(order)

    auth.record_audit(user, "PAYMENT", "Payment", payment.id,
                      f"{payment.number}: {amount} against {order.number} via {method}")
    return payment


def outstanding_balance(customer=None):
    """Total unpaid across shipped and confirmed orders — receivables at a glance."""
    query = SalesOrder.select().where(
        (SalesOrder.status != SalesStatus.CANCELLED)
        & (SalesOrder.payment_status != PaymentStatus.PAID)
    )
    if customer is not None:
        query = query.where(SalesOrder.customer == customer)
    return sum((order.balance_due for order in query), ZERO)


# --- Customer returns ---------------------------------------------------------------

def create_customer_return(customer, warehouse, lines, user=None, order=None,
                           reason=None, restock=True, notes=None) -> CustomerReturn:
    """Take goods back from a customer, optionally returning them to stock.

    A linked order constrains each item to the quantity actually shipped minus earlier
    returns. Serialized units must currently be shipped; restocked units become
    IN_STOCK again, while non-restocked units become SCRAPPED.
    """
    auth.require(user, auth.PERM_CREATE_SALES, "accept a customer return")
    if customer is None or warehouse is None:
        raise SalesError("Choose the customer and warehouse for this return.")
    if order is not None and (order.customer_id != customer.id
                              or order.warehouse_id != warehouse.id):
        raise SalesError(
            "The linked sales order must belong to the selected customer and warehouse.")

    prepared = []
    requested = {}
    serial_keys = set()
    for line in lines:
        quantity = _dec(line.get("quantity"))
        if not quantity.is_finite() or quantity <= 0:
            continue
        item = line.get("item")
        if item is None:
            raise SalesError("Every customer return line needs an item.")
        serials = [str(value).strip() for value in (line.get("serials") or [])
                   if str(value).strip()]
        if len(serials) != len(set(serials)):
            raise SalesError(f"{item.name}: each serial number can appear only once.")
        if item.is_serial_tracked:
            if quantity != quantity.to_integral_value() or len(serials) != int(quantity):
                raise SalesError(
                    f"{item.name}: enter one serial number for each returned unit.")
            serial_records = []
            for number in serials:
                key = (item.id, number)
                if key in serial_keys:
                    raise SalesError(f"Serial {number} appears on more than one line.")
                serial_keys.add(key)
                serial = Serial.get_or_none(
                    (Serial.item == item) & (Serial.serial_number == number))
                if serial is None or serial.status != SerialStatus.SHIPPED:
                    raise SalesError(
                        f"Serial {number} is not recorded as shipped for {item.name}.")
                serial_records.append(serial)
        else:
            if serials:
                raise SalesError(f"{item.name} is not configured for serial tracking.")
            serial_records = []
        entry = dict(line)
        entry.update({"item": item, "quantity": quantity, "serials": serials,
                      "serial_records": serial_records})
        prepared.append(entry)
        requested[item.id] = requested.get(item.id, ZERO) + quantity
    if not prepared:
        raise SalesError("A customer return needs at least one line.")

    if order is not None:
        shipped = {}
        for order_line in order.lines:
            shipped[order_line.item_id] = (
                shipped.get(order_line.item_id, ZERO) + _dec(order_line.shipped_quantity))
        previous = {}
        prior_lines = (CustomerReturnLine.select(CustomerReturnLine)
                       .join(CustomerReturn)
                       .where(CustomerReturn.order == order))
        for line in prior_lines:
            previous[line.item_id] = previous.get(line.item_id, ZERO) + _dec(line.quantity)
        for item_id, quantity in requested.items():
            remaining = shipped.get(item_id, ZERO) - previous.get(item_id, ZERO)
            if quantity > remaining:
                item = next(entry["item"] for entry in prepared
                            if entry["item"].id == item_id)
                raise SalesError(
                    f"{item.name}: only {inventory.fmt_qty(max(ZERO, remaining))} "
                    "shipped unit(s) remain eligible for return on this order.")

    with db.atomic():
        record = CustomerReturn.create(
            number=numbering.next_number("CUSTOMER_RETURN"),
            customer=customer, warehouse=warehouse, order=order,
            return_date=datetime.date.today(), reason=reason, restock=restock,
            notes=notes, created_by=user,
        )
        total = ZERO
        for entry in prepared:
            item = entry["item"]
            quantity = entry["quantity"]
            unit_price = _dec(entry.get("unit_price", item.selling_price))
            unit_cost = _dec(item.avg_cost)

            lot = entry.get("lot")
            if restock:
                if item.is_lot_tracked and lot is None:
                    lot = inventory.get_or_create_lot(
                        item, f"RET-{record.number.split('/')[-1]}",
                        unit_cost=unit_cost,
                    )
                inventory.apply_movement(
                    item, warehouse, Direction.IN, quantity, DocType.CUSTOMER_RETURN,
                    doc_number=record.number, doc_id=record.id, unit_cost=unit_cost,
                    lot=lot, user=user, customer=customer,
                    notes=reason or "Returned by customer",
                )
                inventory.receive_serials(
                    item, warehouse, entry["serials"], lot=lot, unit_cost=unit_cost)
            else:
                for serial in entry["serial_records"]:
                    serial.status = SerialStatus.SCRAPPED
                    serial.warehouse = None
                    serial.save()

            CustomerReturnLine.create(
                customer_return=record, item=item, quantity=quantity,
                unit_price=unit_price, unit_cost=unit_cost, lot=lot)
            total += quantity * unit_price

        record.total = total
        record.save()

    auth.record_audit(
        user, "CREATE", "CustomerReturn", record.id,
        f"{record.number}: {len(prepared)} line(s) from {customer.name}"
        + ("" if restock else " (scrapped, not restocked)"),
    )
    return record


# --- Queries ------------------------------------------------------------------------

def open_orders(warehouse=None, customer=None):
    query = SalesOrder.select().where(SalesOrder.status.in_(SalesStatus.OPEN))
    if warehouse is not None:
        query = query.where(SalesOrder.warehouse == warehouse)
    if customer is not None:
        query = query.where(SalesOrder.customer == customer)
    return list(query.order_by(SalesOrder.promised_date.asc()))


def delete_order(order: SalesOrder, user=None):
    """Delete a draft order. Confirmed or shipped orders are kept for the audit trail."""
    auth.require(user, auth.PERM_DELETE_RECORDS, "delete records")
    if order.status != SalesStatus.DRAFT:
        raise SalesError(
            f"{order.number} is {SalesStatus.LABELS.get(order.status, order.status)} "
            "and can no longer be deleted. Cancel it instead."
        )
    number = order.number
    with db.atomic():
        order.delete_instance(recursive=True)
    auth.record_audit(user, "DELETE", "SalesOrder", None, f"Deleted draft {number}")
