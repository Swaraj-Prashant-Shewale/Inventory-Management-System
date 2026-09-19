"""Direct lab check-in/check-out operations for the web workflow."""
import datetime
import uuid
from decimal import Decimal, InvalidOperation

from peewee import JOIN

from database.connection import db
from database.models import (
    Direction,
    DocType,
    Item,
    Serial,
    SerialStatus,
    StockMovement,
    User,
    Warehouse,
)
from services import auth, inventory


class LabStockError(ValueError):
    """A lab movement was invalid; the message is safe to show to a user."""


def parse_quantity(value, item=None):
    try:
        quantity = Decimal(str(value or "").strip())
    except (InvalidOperation, ValueError):
        raise LabStockError("Enter a valid quantity.")
    if not quantity.is_finite() or quantity <= 0:
        raise LabStockError("Quantity must be greater than zero.")
    if quantity.as_tuple().exponent < -3:
        raise LabStockError("Quantity can have at most three decimal places.")
    if item is not None and item.base_uom and not item.base_uom.allow_decimal \
            and quantity != quantity.to_integral_value():
        raise LabStockError(f"{item.name} must be moved in whole units.")
    return quantity


def resolve_item(code):
    code = (code or "").strip()
    if not code:
        raise LabStockError("Scan or enter an item SKU or barcode.")
    item = Item.get_or_none(
        (Item.barcode == code) | (Item.sku == code.upper())
    )
    if item is None or not item.is_active:
        raise LabStockError(f"No active item matches '{code}'.")
    return item


def resolve_warehouse(warehouse_id):
    try:
        warehouse_id = int(warehouse_id)
    except (TypeError, ValueError):
        raise LabStockError("Choose a warehouse.")
    warehouse = Warehouse.get_or_none(
        (Warehouse.id == warehouse_id) & (Warehouse.is_active == True)  # noqa: E712
    )
    if warehouse is None:
        raise LabStockError("The selected warehouse is not available.")
    return warehouse


def parse_serials(value):
    values = [
        part.strip() for part in (value or "").replace("\r", "\n")
        .replace(",", "\n").split("\n") if part.strip()
    ]
    if len(values) != len(set(values)):
        raise LabStockError("Each serial number can appear only once.")
    return values


def _validate_serials(item, warehouse, direction, quantity, serials):
    if not item.is_serial_tracked:
        if serials:
            raise LabStockError(f"{item.name} is not configured for serial tracking.")
        return
    if quantity != quantity.to_integral_value():
        raise LabStockError("Serial-tracked items must be moved in whole units.")
    if len(serials) != int(quantity):
        raise LabStockError(
            f"Enter {int(quantity)} serial number(s), one for each unit."
        )
    if direction == Direction.OUT:
        for number in serials:
            serial = Serial.get_or_none(
                (Serial.item == item) & (Serial.serial_number == number)
            )
            if serial is None:
                raise LabStockError(f"Serial {number} is not registered for {item.name}.")
            if serial.status != SerialStatus.IN_STOCK \
                    or serial.warehouse_id != warehouse.id:
                raise LabStockError(
                    f"Serial {number} is not in stock at {warehouse.name}."
                )


def prepare(direction, item_code, warehouse_id, quantity, *, lot_number=None,
            expiry_date=None, serials=None, unit_cost=None, notes=None, user=None):
    if direction not in (Direction.IN, Direction.OUT):
        raise LabStockError("Choose Check In or Check Out.")
    permission = (auth.PERM_RECEIVE_GOODS if direction == Direction.IN
                  else auth.PERM_FULFILL_GOODS)
    auth.require(
        user,
        permission,
        "check items in" if direction == Direction.IN else "check items out",
    )

    item = resolve_item(item_code)
    warehouse = resolve_warehouse(warehouse_id)
    quantity = parse_quantity(quantity, item)
    serials = parse_serials(serials)
    _validate_serials(item, warehouse, direction, quantity, serials)

    lot_number = (lot_number or "").strip()
    if item.is_lot_tracked and direction == Direction.IN and not lot_number:
        raise LabStockError(f"Enter a batch number for {item.name}.")
    if lot_number and not item.is_lot_tracked:
        raise LabStockError(f"{item.name} is not configured for batch tracking.")

    expiry = None
    if expiry_date:
        try:
            expiry = datetime.date.fromisoformat(str(expiry_date))
        except ValueError:
            raise LabStockError("Enter a valid expiry date.")
    if expiry and not lot_number:
        raise LabStockError("Enter a batch number with the expiry date.")

    cost = None
    if unit_cost not in (None, ""):
        try:
            cost = Decimal(str(unit_cost).strip())
        except (InvalidOperation, ValueError):
            raise LabStockError("Enter a valid unit cost.")
        if not cost.is_finite() or cost < 0:
            raise LabStockError("Unit cost cannot be negative.")
    if direction == Direction.OUT:
        cost = None

    current = inventory.on_hand(item, warehouse)
    if direction == Direction.OUT and quantity > current:
        raise inventory.InsufficientStock(item, warehouse, quantity, current)

    return {
        "direction": direction,
        "item": item,
        "warehouse": warehouse,
        "quantity": quantity,
        "lot_number": lot_number,
        "expiry_date": expiry,
        "serials": serials,
        "unit_cost": cost,
        "notes": (notes or "").strip()[:500] or None,
        "current": current,
        "resulting": current + quantity if direction == Direction.IN else current - quantity,
        "user": user,
    }


def commit(prepared):
    """Commit a prepared movement, rechecking stock inside the transaction."""
    direction = prepared["direction"]
    item = prepared["item"]
    warehouse = prepared["warehouse"]
    quantity = prepared["quantity"]
    serials = prepared["serials"]
    user = prepared["user"]
    doc_type = (DocType.LAB_CHECK_IN if direction == Direction.IN
                else DocType.LAB_CHECK_OUT)
    prefix = "LAB-IN" if direction == Direction.IN else "LAB-OUT"
    doc_number = (
        f"{prefix}-{datetime.datetime.now():%Y%m%d-%H%M%S}-"
        f"{uuid.uuid4().hex[:6].upper()}"
    )

    with db.atomic():
        lot = None
        if direction == Direction.IN and item.is_lot_tracked:
            lot = inventory.get_or_create_lot(
                item,
                prepared["lot_number"],
                expiry_date=prepared["expiry_date"],
                unit_cost=prepared["unit_cost"] or item.avg_cost,
                received_date=datetime.date.today(),
            )

        if direction == Direction.IN and prepared["unit_cost"] is not None:
            inventory.update_moving_average(item, quantity, prepared["unit_cost"])

        if direction == Direction.OUT and item.is_lot_tracked:
            allocations = inventory.allocate_fefo(item, warehouse, quantity)
        else:
            allocations = [(lot, quantity)]

        movements = []
        for allocated_lot, allocated_quantity in allocations:
            movements.append(inventory.apply_movement(
                item,
                warehouse,
                direction,
                allocated_quantity,
                doc_type,
                doc_number=doc_number,
                unit_cost=item.avg_cost,
                lot=allocated_lot,
                user=user,
                employee=user.employee if user else None,
                notes=prepared["notes"],
            ))

        if direction == Direction.IN:
            inventory.receive_serials(
                item, warehouse, serials, lot=lot, unit_cost=item.avg_cost
            )
        else:
            inventory.ship_serials(item, serials)

    verb = "checked in" if direction == Direction.IN else "checked out"
    auth.record_audit(
        user,
        direction,
        "StockMovement",
        movements[0].id,
        f"{item.name}: {inventory.fmt_qty(quantity)} {verb} at {warehouse.name}",
    )
    return doc_number, movements


def recent(limit=50):
    return list(
        StockMovement.select(StockMovement, Item, Warehouse, User)
        .join(Item)
        .switch(StockMovement)
        .join(Warehouse)
        .switch(StockMovement)
        .join(User, JOIN.LEFT_OUTER)
        .where(StockMovement.doc_type.in_(
            (DocType.LAB_CHECK_IN, DocType.LAB_CHECK_OUT)
        ))
        .order_by(StockMovement.timestamp.desc(), StockMovement.id.desc())
        .limit(limit)
    )
