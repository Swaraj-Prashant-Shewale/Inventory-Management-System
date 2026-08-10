"""The stock engine.

Every quantity change in the application goes through `apply_movement`, which is the only
function permitted to touch StockLevel. That single choke point is what guarantees the
Recent Logs trail can never disagree with the on-hand numbers.

Costing is moving average, recalculated on each receipt and held per item across all
warehouses. Lot-tracked items are consumed earliest-expiry-first (FEFO).
"""
import datetime
from decimal import Decimal

from peewee import fn

from database.connection import db, is_postgres
from database.models import (
    ZERO,
    Direction,
    DocType,
    Item,
    Lot,
    LotStock,
    Serial,
    SerialStatus,
    StockLevel,
    StockMovement,
    Warehouse,
)


class StockError(Exception):
    """A stock operation was refused. The message is safe to show the user."""


class InsufficientStock(StockError):
    def __init__(self, item, warehouse, requested, available):
        self.item, self.warehouse = item, warehouse
        self.requested, self.available = requested, available
        super().__init__(
            f"Not enough {item.name} at {warehouse.name}: "
            f"{fmt_qty(requested)} requested, only {fmt_qty(available)} on hand."
        )


def fmt_qty(value) -> str:
    """Trim trailing zeros so 5.000 reads as 5 but 2.500 stays 2.5."""
    d = Decimal(str(value or 0)).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return f"{d:f}"


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value if value is not None else 0))


# --- Stock levels -------------------------------------------------------------------

def get_stock_level(item, warehouse, create: bool = True) -> StockLevel:
    level = StockLevel.get_or_none(
        (StockLevel.item == item) & (StockLevel.warehouse == warehouse)
    )
    if level is None and create:
        level, _ = StockLevel.get_or_create(
            item=item, warehouse=warehouse, defaults={"on_hand": ZERO, "reserved": ZERO}
        )
    return level


def lock_stock_level(item, warehouse) -> StockLevel:
    """Return the item+warehouse stock row, locked FOR UPDATE, re-read under the lock.

    Every write to on_hand or reserved is a read-modify-write, and on Postgres two
    documents touching the same item+warehouse hold locks on *different* order lines, so
    they never serialize there. Without locking the StockLevel row itself the second
    writer overwrites the first (a lost update): on-hand goes wrong and an OUT can slip
    past the negative-stock check against a stale balance. Callers must already be inside
    db.atomic() so the lock is held to commit. Re-reading here is the point — the value
    used for the arithmetic must be the one seen *after* the lock is acquired.
    """
    level = get_stock_level(item, warehouse, create=True)
    if is_postgres():
        level = (StockLevel.select()
                 .where(StockLevel.id == level.id)
                 .for_update()
                 .first())
    return level


def on_hand(item, warehouse=None) -> Decimal:
    """On-hand quantity in one warehouse, or across all of them when warehouse is None."""
    query = StockLevel.select(fn.COALESCE(fn.SUM(StockLevel.on_hand), 0)).where(
        StockLevel.item == item
    )
    if warehouse is not None:
        query = query.where(StockLevel.warehouse == warehouse)
    return _dec(query.scalar())


def available(item, warehouse) -> Decimal:
    """On hand minus stock already committed to confirmed sales orders."""
    level = get_stock_level(item, warehouse, create=False)
    if level is None:
        return ZERO
    return _dec(level.on_hand) - _dec(level.reserved)


def total_stock_value() -> Decimal:
    """Sum of on-hand quantity × moving-average cost across every warehouse."""
    rows = (
        StockLevel.select(StockLevel.on_hand, Item.avg_cost)
        .join(Item)
        .where(StockLevel.on_hand > 0)
        .tuples()
    )
    return sum((_dec(qty) * _dec(cost) for qty, cost in rows), ZERO)


# --- The single write path ----------------------------------------------------------

def apply_movement(
    item,
    warehouse,
    direction,
    quantity,
    doc_type,
    *,
    doc_number=None,
    doc_id=None,
    unit_cost=None,
    lot=None,
    user=None,
    employee=None,
    supplier=None,
    customer=None,
    truck_hsrp=None,
    notes=None,
    allow_negative=False,
    timestamp=None,
) -> StockMovement:
    """Move `quantity` of `item` in or out of `warehouse` and record it.

    `quantity` is always positive; `direction` carries the sign. Returns the
    StockMovement row that was written.
    """
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("Quantity must be greater than zero.")
    if direction not in (Direction.IN, Direction.OUT):
        raise StockError(f"Unknown movement direction '{direction}'.")

    with db.atomic():
        level = lock_stock_level(item, warehouse)
        current = _dec(level.on_hand)

        if direction == Direction.IN:
            new_balance = current + quantity
        else:
            new_balance = current - quantity
            if new_balance < 0 and not allow_negative:
                raise InsufficientStock(item, warehouse, quantity, current)

        level.on_hand = new_balance
        level.save()

        if lot is not None:
            _apply_lot_movement(lot, warehouse, direction, quantity, allow_negative)

        return StockMovement.create(
            timestamp=timestamp or datetime.datetime.now(),
            item=item,
            warehouse=warehouse,
            direction=direction,
            quantity=quantity,
            balance_after=new_balance,
            unit_cost=_dec(unit_cost if unit_cost is not None else item.avg_cost),
            doc_type=doc_type,
            doc_number=doc_number,
            doc_id=doc_id,
            lot=lot,
            user=user,
            employee=employee,
            supplier=supplier,
            customer=customer,
            truck_hsrp=truck_hsrp,
            notes=notes,
        )


def _apply_lot_movement(lot, warehouse, direction, quantity, allow_negative):
    lot_stock, _ = LotStock.get_or_create(
        lot=lot, warehouse=warehouse, defaults={"quantity": ZERO}
    )
    if is_postgres():   # same lost-update risk as StockLevel; lock the row
        lot_stock = (LotStock.select()
                     .where(LotStock.id == lot_stock.id)
                     .for_update()
                     .first())
    current = _dec(lot_stock.quantity)
    new_qty = current + quantity if direction == Direction.IN else current - quantity
    if new_qty < 0 and not allow_negative:
        raise StockError(
            f"Lot {lot.lot_number} only has {fmt_qty(current)} at "
            f"{warehouse.name}; cannot remove {fmt_qty(quantity)}."
        )
    lot_stock.quantity = new_qty
    lot_stock.save()


# --- Moving average cost ------------------------------------------------------------

def update_moving_average(item, received_qty, received_unit_cost) -> Decimal:
    """Blend a new receipt into the item's average cost and return the new average.

    new_avg = (existing_qty × old_avg + received_qty × receipt_cost) / total_qty

    Uses total on-hand across all warehouses, because the average is a property of the
    item, not of a location.
    """
    received_qty = _dec(received_qty)
    received_unit_cost = _dec(received_unit_cost)

    with db.atomic():
        # Lock the Item row so two concurrent receipts of the same item serialize here;
        # otherwise both read the same avg_cost, each blend only their own receipt, and
        # the later write silently discards the earlier — corrupting cost basis and COGS.
        # The lock is re-read under FOR UPDATE and held to commit.
        locked = item
        if is_postgres():
            locked = Item.select().where(Item.id == item.id).for_update().first()

        existing_qty = on_hand(locked)
        old_avg = _dec(locked.avg_cost)

        total_qty = existing_qty + received_qty
        if total_qty <= 0:
            # Nothing on hand to blend against (e.g. stock negative) — adopt the new cost.
            new_avg = received_unit_cost
        else:
            new_avg = ((existing_qty * old_avg)
                       + (received_qty * received_unit_cost)) / total_qty

        # 4 guard digits beyond the 2dp display: the stored basis feeds the next receipt's
        # blend, so rounding to the cent here would compound valuation drift over time.
        locked.avg_cost = new_avg.quantize(Decimal("0.0001"))
        locked.last_purchase_cost = received_unit_cost
        locked.save()

    # Mirror onto the caller's object so it sees the fresh values without a reload.
    item.avg_cost = locked.avg_cost
    item.last_purchase_cost = locked.last_purchase_cost
    return item.avg_cost


# --- Lots and FEFO ------------------------------------------------------------------

def get_or_create_lot(item, lot_number, expiry_date=None, supplier=None, unit_cost=None,
                      received_date=None) -> Lot:
    lot = Lot.get_or_none((Lot.item == item) & (Lot.lot_number == lot_number))
    if lot is not None:
        # Fill in details that were unknown when the lot was first seen.
        changed = False
        if expiry_date and not lot.expiry_date:
            lot.expiry_date, changed = expiry_date, True
        if supplier and not lot.supplier:
            lot.supplier, changed = supplier, True
        if changed:
            lot.save()
        return lot

    if expiry_date is None and item.shelf_life_days:
        base = received_date or datetime.date.today()
        expiry_date = base + datetime.timedelta(days=item.shelf_life_days)

    return Lot.create(
        item=item,
        lot_number=lot_number,
        expiry_date=expiry_date,
        supplier=supplier,
        unit_cost=_dec(unit_cost),
        received_date=received_date or datetime.date.today(),
    )


def available_lots(item, warehouse):
    """Lots with stock in this warehouse, earliest expiry first (FEFO).

    Lots with no expiry date sort last — a dated batch should always leave before an
    undated one.
    """
    return list(
        LotStock.select(LotStock, Lot)
        .join(Lot)
        .where(
            (Lot.item == item)
            & (LotStock.warehouse == warehouse)
            & (LotStock.quantity > 0)
        )
        .order_by(Lot.expiry_date.is_null(), Lot.expiry_date.asc(), Lot.id.asc())
    )


def allocate_fefo(item, warehouse, quantity):
    """Split `quantity` across available lots, earliest expiry first.

    Returns [(Lot, quantity), ...]. Raises InsufficientStock if the lots can't cover it.
    """
    quantity = _dec(quantity)
    remaining = quantity
    allocation = []

    for lot_stock in available_lots(item, warehouse):
        if remaining <= 0:
            break
        take = min(remaining, _dec(lot_stock.quantity))
        if take > 0:
            allocation.append((lot_stock.lot, take))
            remaining -= take

    if remaining > 0:
        covered = quantity - remaining
        raise InsufficientStock(item, warehouse, quantity, covered)

    return allocation


def expiring_lots(within_days: int = 30, warehouse=None):
    """Lots holding stock that expire within `within_days`. Feeds the expiry reminders."""
    cutoff = datetime.date.today() + datetime.timedelta(days=within_days)
    # Warehouse is joined because the reminder reads its name — otherwise one extra query
    # per expiring lot against the remote database.
    query = (
        LotStock.select(LotStock, Lot, Item, Warehouse)
        .join(Lot)
        .join(Item)
        .switch(LotStock)
        .join(Warehouse)
        .where(
            (LotStock.quantity > 0)
            & (Lot.expiry_date.is_null(False))
            & (Lot.expiry_date <= cutoff)
        )
    )
    if warehouse is not None:
        query = query.where(LotStock.warehouse == warehouse)
    return list(query.order_by(Lot.expiry_date.asc()))


# --- Serial numbers -----------------------------------------------------------------

def receive_serials(item, warehouse, serial_numbers, lot=None, unit_cost=None):
    """Register serial-tracked units arriving into stock."""
    if not item.is_serial_tracked:
        return []
    created = []
    for raw in serial_numbers:
        number = (raw or "").strip()
        if not number:
            continue
        existing = Serial.get_or_none(
            (Serial.item == item) & (Serial.serial_number == number)
        )
        if existing is not None:
            if existing.status == SerialStatus.IN_STOCK:
                raise StockError(
                    f"Serial {number} for {item.name} is already in stock at "
                    f"{existing.warehouse.name if existing.warehouse else 'an unknown location'}."
                )
            existing.status = SerialStatus.IN_STOCK
            existing.warehouse = warehouse
            existing.lot = lot
            existing.received_on = datetime.date.today()
            existing.save()
            created.append(existing)
            continue
        created.append(
            Serial.create(
                item=item, serial_number=number, lot=lot, warehouse=warehouse,
                status=SerialStatus.IN_STOCK, unit_cost=_dec(unit_cost),
                received_on=datetime.date.today(),
            )
        )
    return created


def ship_serials(item, serial_numbers):
    """Mark serial-tracked units as gone. Rejects anything not actually in stock."""
    shipped = []
    for raw in serial_numbers:
        number = (raw or "").strip()
        if not number:
            continue
        serial = Serial.get_or_none(
            (Serial.item == item) & (Serial.serial_number == number)
        )
        if serial is None:
            raise StockError(f"Serial {number} is not registered for {item.name}.")
        if serial.status != SerialStatus.IN_STOCK:
            raise StockError(f"Serial {number} is not in stock (status: {serial.status}).")
        serial.status = SerialStatus.SHIPPED
        serial.shipped_on = datetime.date.today()
        serial.save()
        shipped.append(serial)
    return shipped


# --- Reservations -------------------------------------------------------------------

def reserve(item, warehouse, quantity, enforce: bool = True):
    """Commit stock to a confirmed sales order."""
    quantity = _dec(quantity)
    if quantity <= 0:
        return
    with db.atomic():
        level = lock_stock_level(item, warehouse)
        free = _dec(level.on_hand) - _dec(level.reserved)
        if enforce and quantity > free:
            raise InsufficientStock(item, warehouse, quantity, free)
        level.reserved = _dec(level.reserved) + quantity
        level.save()


def release_reservation(item, warehouse, quantity):
    """Free stock previously reserved — on shipment or cancellation."""
    quantity = _dec(quantity)
    if quantity <= 0:
        return
    with db.atomic():
        level = lock_stock_level(item, warehouse)
        # Never let a double-release push reserved below zero.
        level.reserved = max(ZERO, _dec(level.reserved) - quantity)
        level.save()


# --- Opening balances and reorder logic ---------------------------------------------

def set_opening_balance(item, warehouse, quantity, unit_cost=None, user=None,
                        lot_number=None, expiry_date=None):
    """Establish a starting quantity for an item, e.g. during data import."""
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("Opening balance must be greater than zero.")

    lot = None
    if item.is_lot_tracked:
        lot = get_or_create_lot(
            item, lot_number or "OPENING", expiry_date=expiry_date, unit_cost=unit_cost
        )

    with db.atomic():
        if unit_cost is not None:
            update_moving_average(item, quantity, unit_cost)
        return apply_movement(
            item, warehouse, Direction.IN, quantity, DocType.OPENING,
            doc_number="OPENING", unit_cost=unit_cost or item.avg_cost, lot=lot,
            user=user, notes="Opening balance",
        )


def below_minimum(warehouse=None):
    """Stock levels at or under their minimum — drives reorder and low-stock alerts."""
    query = (
        StockLevel.select(StockLevel, Item)
        .join(Item)
        .where(Item.is_active == True)  # noqa: E712 - peewee needs ==, not `is`
    )
    if warehouse is not None:
        query = query.where(StockLevel.warehouse == warehouse)

    result = []
    for level in query:
        minimum = _dec(level.effective_min)
        if minimum > 0 and _dec(level.on_hand) <= minimum:
            result.append(level)
    return result


def suggested_order_quantity(level: StockLevel, to_maximum: bool = True) -> Decimal:
    """How much to buy to refill this stock level.

    `to_maximum=True` fills to the maximum level ("Order Max"); otherwise it buys just
    enough to clear the minimum.
    """
    target = _dec(level.effective_max) if to_maximum else _dec(level.effective_min)
    shortfall = target - _dec(level.on_hand)
    if shortfall <= 0:
        return ZERO

    # Round up to whole purchase packs so we order what the supplier actually sells.
    pack = _dec(level.item.pack_size) or Decimal("1")
    if pack > 1:
        packs = (shortfall / pack).to_integral_value(rounding="ROUND_CEILING")
        shortfall = packs * pack

    uom = level.item.base_uom
    if uom is not None and not uom.allow_decimal:
        shortfall = shortfall.to_integral_value(rounding="ROUND_CEILING")
    return shortfall


def warehouse_choices(active_only: bool = True):
    query = Warehouse.select().order_by(Warehouse.name)
    if active_only:
        query = query.where(Warehouse.is_active == True)  # noqa: E712
    return list(query)
