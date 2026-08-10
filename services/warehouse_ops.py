"""Stock adjustments, warehouse transfers and cycle counts.

These are the operations that keep the system honest against physical reality. All three
are two-step — recorded first, then approved or received — so a mistake is caught before
it moves stock, and so someone's name is against the variance.
"""
import datetime
from decimal import Decimal

from database.connection import db
from database.models import (
    ZERO,
    AdjustmentReason,
    CountStatus,
    CycleCount,
    CycleCountLine,
    Direction,
    DocType,
    Lot,
    StockAdjustment,
    StockAdjustmentLine,
    StockLevel,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    TransferStatus,
)
from services import auth, inventory, numbering


class OperationError(Exception):
    """A warehouse operation was refused. Safe to show the user."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


# --- Stock adjustments --------------------------------------------------------------

def create_adjustment(warehouse, lines, reason=AdjustmentReason.OTHER, user=None,
                      notes=None, adjustment_date=None) -> StockAdjustment:
    """Record a proposed write-up or write-down. Stock does not move until approved.

    `lines`: [{item, quantity_delta (signed), lot (optional), note (optional)}]
    """
    auth.require(user, auth.PERM_ADJUST_STOCK, "adjust stock")
    lines = [line for line in lines if _dec(line.get("quantity_delta")) != 0]
    if not lines:
        raise OperationError("An adjustment needs at least one line with a quantity.")

    with db.atomic():
        adjustment = StockAdjustment.create(
            number=numbering.next_number("STOCK_ADJUSTMENT"),
            warehouse=warehouse,
            adjustment_date=adjustment_date or datetime.date.today(),
            reason=reason,
            notes=notes,
            created_by=user,
        )
        for line in lines:
            item = line["item"]
            StockAdjustmentLine.create(
                adjustment=adjustment, item=item,
                quantity_delta=_dec(line["quantity_delta"]),
                unit_cost=_dec(line.get("unit_cost", item.avg_cost)),
                lot=line.get("lot"), note=line.get("note"),
            )

    auth.record_audit(user, "CREATE", "StockAdjustment", adjustment.id,
                      f"{adjustment.number}: {len(lines)} line(s) awaiting approval")
    return adjustment


def approve_adjustment(adjustment: StockAdjustment, user=None) -> StockAdjustment:
    """Approve an adjustment and move the stock."""
    if adjustment.approved_at is not None:
        raise OperationError(f"{adjustment.number} has already been approved.")
    auth.require(user, auth.PERM_APPROVE_ADJUSTMENT)

    lines = list(adjustment.lines)
    if not lines:
        raise OperationError(f"{adjustment.number} has no lines.")

    with db.atomic():
        for line in lines:
            delta = _dec(line.quantity_delta)
            direction = Direction.IN if delta > 0 else Direction.OUT
            inventory.apply_movement(
                line.item, adjustment.warehouse, direction, abs(delta),
                DocType.ADJUSTMENT, doc_number=adjustment.number, doc_id=adjustment.id,
                unit_cost=line.unit_cost, lot=line.lot, user=user,
                employee=user.employee if user else None,
                notes=(line.note or AdjustmentReason.LABELS.get(adjustment.reason,
                                                               adjustment.reason)),
            )
        adjustment.approved_by = user
        adjustment.approved_at = datetime.datetime.now()
        adjustment.save()

    auth.record_audit(user, "APPROVE", "StockAdjustment", adjustment.id,
                      f"Approved {adjustment.number} ({len(lines)} lines)")
    return adjustment


def adjustment_value(adjustment: StockAdjustment) -> Decimal:
    """Net value of the adjustment — what the write-off actually costs."""
    return sum(
        (_dec(line.quantity_delta) * _dec(line.unit_cost) for line in adjustment.lines),
        ZERO,
    )


# --- Transfers ----------------------------------------------------------------------

def create_transfer(from_warehouse, to_warehouse, lines, user=None, truck_hsrp=None,
                    notes=None) -> StockTransfer:
    """Draft a warehouse-to-warehouse transfer. Stock moves on dispatch, not now.

    `lines`: [{item, quantity, lot (optional)}]
    """
    auth.require(user, auth.PERM_TRANSFER_STOCK, "transfer stock")
    if from_warehouse is None or to_warehouse is None:
        raise OperationError("Choose both a source and a destination warehouse.")
    if from_warehouse.id == to_warehouse.id:
        raise OperationError("The source and destination warehouses must be different.")

    lines = [line for line in lines if _dec(line.get("quantity")) > 0]
    if not lines:
        raise OperationError("A transfer needs at least one line with a quantity.")

    with db.atomic():
        transfer = StockTransfer.create(
            number=numbering.next_number("STOCK_TRANSFER"),
            from_warehouse=from_warehouse, to_warehouse=to_warehouse,
            status=TransferStatus.DRAFT, truck_hsrp=truck_hsrp, notes=notes,
            created_by=user,
        )
        for line in lines:
            StockTransferLine.create(
                transfer=transfer, item=line["item"],
                quantity_sent=_dec(line["quantity"]), lot=line.get("lot"),
            )

    auth.record_audit(user, "CREATE", "StockTransfer", transfer.id,
                      f"{transfer.number}: {from_warehouse.name} → {to_warehouse.name}")
    return transfer


def dispatch_transfer(transfer: StockTransfer, user=None, truck_hsrp=None,
                      dispatch_date=None) -> StockTransfer:
    """Send the goods: stock leaves the source warehouse and is in transit."""
    auth.require(user, auth.PERM_TRANSFER_STOCK, "dispatch a transfer")
    if transfer.status != TransferStatus.DRAFT:
        raise OperationError(
            f"{transfer.number} is "
            f"{TransferStatus.LABELS.get(transfer.status, transfer.status)}."
        )

    with db.atomic():
        for line in transfer.lines:
            item = line.item
            quantity = _dec(line.quantity_sent)
            lot = line.lot
            if item.is_lot_tracked and lot is None:
                allocation = inventory.allocate_fefo(
                    item, transfer.from_warehouse, quantity
                )
                if len(allocation) == 1:
                    lot = allocation[0][0]
                    line.lot = lot
                    line.save()
                else:
                    # Split across batches: post one movement per lot.
                    for batch, batch_quantity in allocation:
                        inventory.apply_movement(
                            item, transfer.from_warehouse, Direction.OUT, batch_quantity,
                            DocType.TRANSFER_OUT, doc_number=transfer.number,
                            doc_id=transfer.id, lot=batch, user=user,
                            truck_hsrp=truck_hsrp or transfer.truck_hsrp,
                            notes=f"To {transfer.to_warehouse.name}",
                        )
                    continue

            inventory.apply_movement(
                item, transfer.from_warehouse, Direction.OUT, quantity,
                DocType.TRANSFER_OUT, doc_number=transfer.number, doc_id=transfer.id,
                unit_cost=item.avg_cost, lot=lot, user=user,
                employee=user.employee if user else None,
                truck_hsrp=truck_hsrp or transfer.truck_hsrp,
                notes=f"To {transfer.to_warehouse.name}",
            )

        transfer.status = TransferStatus.IN_TRANSIT
        transfer.dispatch_date = dispatch_date or datetime.date.today()
        if truck_hsrp:
            transfer.truck_hsrp = truck_hsrp
        transfer.save()

    auth.record_audit(user, "DISPATCH", "StockTransfer", transfer.id,
                      f"Dispatched {transfer.number}")
    return transfer


def _received_lot_allocation(transfer, line, quantity):
    """How to credit destination lots for `quantity` received on `line`.

    Returns [(lot_or_None, quantity)]. For a non-lot item, one entry with lot=None. For a
    lot-tracked item, read back the TRANSFER_OUT movements this dispatch actually posted
    (each carries its lot) and apportion the received quantity across them earliest-first,
    so the destination LotStock mirrors what physically travelled.
    """
    if not line.item.is_lot_tracked:
        return [(None, quantity)]

    dispatched = list(
        StockMovement.select(StockMovement, Lot)
        .join(Lot, on=StockMovement.lot)
        .where((StockMovement.doc_id == transfer.id)
               & (StockMovement.doc_type == DocType.TRANSFER_OUT)
               & (StockMovement.item == line.item)
               & (StockMovement.lot.is_null(False)))
        .order_by(Lot.expiry_date.is_null(), Lot.expiry_date.asc(), StockMovement.id.asc())
    )
    if not dispatched:
        # Non-lot dispatch recorded (shouldn't happen for a lot item) — fall back.
        return [(line.lot, quantity)]

    remaining = quantity
    allocation = []
    for movement in dispatched:
        if remaining <= 0:
            break
        take = min(remaining, _dec(movement.quantity))
        if take > 0:
            allocation.append((movement.lot, take))
            remaining -= take
    if remaining > 0 and allocation:
        # Received more than was dispatched per-lot (rounding) — put the rest on the last.
        last_lot, last_qty = allocation[-1]
        allocation[-1] = (last_lot, last_qty + remaining)
    return allocation


def receive_transfer(transfer, lines=None, user=None, receive_date=None) -> StockTransfer:
    """Book the goods in at the destination.

    `lines`: [{transfer_line, quantity}] — omit to receive everything that was sent.
    A shortfall is recorded on the line; it is a loss in transit and shows as a variance.
    """
    auth.require(user, auth.PERM_TRANSFER_STOCK, "receive a transfer")
    if transfer.status != TransferStatus.IN_TRANSIT:
        raise OperationError(
            f"{transfer.number} is "
            f"{TransferStatus.LABELS.get(transfer.status, transfer.status)} — "
            "only goods in transit can be received."
        )

    if lines is None:
        lines = [{"transfer_line": line, "quantity": _dec(line.quantity_sent)}
                 for line in transfer.lines]

    with db.atomic():
        for entry in lines:
            line = entry["transfer_line"]
            quantity = _dec(entry.get("quantity"))
            if quantity <= 0:
                continue
            if quantity > _dec(line.quantity_sent):
                raise OperationError(
                    f"{line.item.name}: cannot receive more than the "
                    f"{inventory.fmt_qty(line.quantity_sent)} that was sent."
                )

            # A lot-tracked line may have been dispatched from several batches (FEFO
            # split), in which case line.lot is None. Crediting the destination with a
            # single lot=None would raise on-hand without ever crediting LotStock, so the
            # destination's lot quantities would drift below on-hand forever. Replay the
            # exact lots that were dispatched, apportioning a partial receipt across them.
            allocation = _received_lot_allocation(transfer, line, quantity)
            for lot, lot_quantity in allocation:
                inventory.apply_movement(
                    line.item, transfer.to_warehouse, Direction.IN, lot_quantity,
                    DocType.TRANSFER_IN, doc_number=transfer.number, doc_id=transfer.id,
                    unit_cost=line.item.avg_cost, lot=lot, user=user,
                    employee=user.employee if user else None,
                    truck_hsrp=transfer.truck_hsrp,
                    notes=f"From {transfer.from_warehouse.name}",
                )
            line.quantity_received = quantity
            line.save()

        transfer.status = TransferStatus.RECEIVED
        transfer.received_date = receive_date or datetime.date.today()
        transfer.save()

    auth.record_audit(user, "RECEIVE", "StockTransfer", transfer.id,
                      f"Received {transfer.number} at {transfer.to_warehouse.name}")
    return transfer


def in_transit_value() -> Decimal:
    """Value of stock dispatched but not yet received — invisible to both warehouses."""
    total = ZERO
    for transfer in StockTransfer.select().where(
        StockTransfer.status == TransferStatus.IN_TRANSIT
    ):
        for line in transfer.lines:
            total += _dec(line.quantity_sent) * _dec(line.item.avg_cost)
    return total


# --- Cycle counts -------------------------------------------------------------------

def open_cycle_count(warehouse, items=None, user=None, notes=None) -> CycleCount:
    """Start a count, snapshotting what the system currently believes is on the shelf.

    `items` defaults to everything with a stock record in that warehouse.
    """
    auth.require(user, auth.PERM_CYCLE_COUNT, "start a cycle count")
    with db.atomic():
        count = CycleCount.create(
            number=numbering.next_number("CYCLE_COUNT"),
            warehouse=warehouse, status=CountStatus.OPEN,
            count_date=datetime.date.today(), counted_by=user, notes=notes,
        )

        if items is None:
            levels = list(StockLevel.select().where(StockLevel.warehouse == warehouse))
        else:
            levels = [inventory.get_stock_level(item, warehouse) for item in items]

        if not levels:
            raise OperationError(
                f"{warehouse.name} has no stock records to count yet."
            )

        for level in levels:
            CycleCountLine.create(
                count=count, item=level.item,
                expected_quantity=_dec(level.on_hand), counted_quantity=None,
            )

    auth.record_audit(user, "CREATE", "CycleCount", count.id,
                      f"{count.number}: opened over {len(levels)} item(s) "
                      f"at {warehouse.name}")
    return count


def record_counts(count: CycleCount, values, user=None) -> CycleCount:
    """Save counted quantities. `values` maps CycleCountLine id → counted quantity."""
    # These numbers become the variance adjustment that moves stock, so the write must be
    # authorized like every other in this module — and never accepted from user=None.
    auth.require(user, auth.PERM_CYCLE_COUNT, "record a cycle count")
    if count.status not in (CountStatus.OPEN, CountStatus.COUNTED):
        raise OperationError(
            f"{count.number} is "
            f"{CountStatus.LABELS.get(count.status, count.status)} and cannot be edited."
        )

    with db.atomic():
        for line in count.lines:
            if line.id in values and values[line.id] is not None:
                line.counted_quantity = _dec(values[line.id])
                line.save()
        if any(line.counted_quantity is not None for line in count.lines):
            count.status = CountStatus.COUNTED
        count.counted_by = user or count.counted_by
        count.save()
    return count


def count_variances(count: CycleCount):
    """Counted lines that disagree with the system, as [(line, variance), ...]."""
    result = []
    for line in count.lines:
        if line.counted_quantity is None:
            continue
        variance = _dec(line.counted_quantity) - _dec(line.expected_quantity)
        if variance != 0:
            result.append((line, variance))
    return result


def approve_cycle_count(count: CycleCount, user=None) -> StockAdjustment:
    """Accept the count and post an adjustment for every variance.

    Returns the adjustment created, or None when the count matched exactly.
    """
    if count.status == CountStatus.APPROVED:
        raise OperationError(f"{count.number} has already been approved.")
    if count.status == CountStatus.CANCELLED:
        raise OperationError(f"{count.number} was cancelled.")
    auth.require(user, auth.PERM_APPROVE_ADJUSTMENT)

    uncounted = [line for line in count.lines if line.counted_quantity is None]
    if uncounted:
        raise OperationError(
            f"{len(uncounted)} item(s) on {count.number} have not been counted yet. "
            "Enter a quantity for every line, including the ones that are zero."
        )

    variances = count_variances(count)

    with db.atomic():
        adjustment = None
        if variances:
            adjustment = create_adjustment(
                count.warehouse,
                [{"item": line.item, "quantity_delta": variance, "lot": line.lot,
                  "note": f"Counted {inventory.fmt_qty(line.counted_quantity)} vs "
                          f"expected {inventory.fmt_qty(line.expected_quantity)}"}
                 for line, variance in variances],
                reason=AdjustmentReason.COUNT,
                user=user,
                notes=f"Variance from cycle count {count.number}",
            )
            approve_adjustment(adjustment, user=user)

        count.status = CountStatus.APPROVED
        count.approved_by = user
        count.approved_at = datetime.datetime.now()
        count.save()

    auth.record_audit(
        user, "APPROVE", "CycleCount", count.id,
        f"Approved {count.number}: {len(variances)} variance(s)"
        + (f" posted as {adjustment.number}" if adjustment else " — count matched"),
    )
    return adjustment
