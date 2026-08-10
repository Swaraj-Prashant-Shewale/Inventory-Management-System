"""Automatic reminders: low stock, overdue purchase orders and lots nearing expiry.

These sit alongside the reminders people type by hand. Each generated reminder carries
its source and the record it came from, so re-running the generator updates the existing
one rather than piling up a duplicate every time the app opens.
"""
import datetime
import logging
from decimal import Decimal

from peewee import JOIN

from database.connection import db
from database.models import (
    Item,
    Reminder,
    ReminderSource,
    StockLevel,
    Supplier,
    Warehouse,
)
from services import inventory, purchasing

log = logging.getLogger(__name__)

LOW_STOCK = "StockLevel"
PURCHASE_ORDER = "PurchaseOrder"
LOT = "Lot"

EXPIRY_HORIZON_DAYS = 30


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _upsert(source, ref_type, ref_id, title, details, due_date):
    """Create the reminder, or refresh the wording of the open one already there.

    Also self-heals: if a prior concurrent run created duplicate open reminders for the
    same underlying record, keep the earliest and tick the rest off, so duplicates
    collapse over time rather than accumulating.
    """
    matches = list(Reminder.select().where(
        (Reminder.source == source)
        & (Reminder.ref_type == ref_type)
        & (Reminder.ref_id == ref_id)
        & (Reminder.is_done == False)  # noqa: E712
    ).order_by(Reminder.id))

    if matches:
        existing = matches[0]
        for duplicate in matches[1:]:
            duplicate.is_done = True
            duplicate.save()
        changed = False
        if existing.title != title:
            existing.title, changed = title, True
        if existing.details != details:
            existing.details, changed = details, True
        if existing.due_date != due_date:
            existing.due_date, changed = due_date, True
        if changed:
            existing.save()
        return existing, False

    return Reminder.create(
        source=source, ref_type=ref_type, ref_id=ref_id, title=title,
        details=details, due_date=due_date,
    ), True


def _clear_resolved(source, ref_type, live_ids):
    """Tick off generated reminders whose underlying problem has gone away."""
    query = Reminder.select().where(
        (Reminder.source == source)
        & (Reminder.ref_type == ref_type)
        & (Reminder.is_done == False)  # noqa: E712
    )
    resolved = 0
    for reminder in query:
        if reminder.ref_id not in live_ids:
            reminder.is_done = True
            reminder.save()
            resolved += 1
    return resolved


# --- Individual generators ----------------------------------------------------------

def low_stock_reminders(warehouse=None):
    """One reminder per item that has fallen to or below its minimum."""
    today = datetime.date.today()
    created = 0
    live = set()

    # Join the preferred supplier too: the details line reads its name, and without the
    # join that is one extra query per low-stock item against the remote database.
    levels = (StockLevel.select(StockLevel, Item, Warehouse, Supplier)
              .join(Item)
              .join(Supplier, JOIN.LEFT_OUTER, on=Item.preferred_supplier)
              .switch(StockLevel).join(Warehouse)
              .where(Item.is_active == True))  # noqa: E712
    if warehouse is not None:
        levels = levels.where(StockLevel.warehouse == warehouse)

    for level in levels:
        minimum = (level.min_level if level.min_level is not None
                   else level.item.default_min_level)
        minimum = _dec(minimum)
        on_hand = _dec(level.on_hand)
        if minimum <= 0 or on_hand > minimum:
            continue

        live.add(level.id)
        _, is_new = _upsert(
            ReminderSource.LOW_STOCK, LOW_STOCK, level.id,
            title=f"{level.item.name} is below minimum at {level.warehouse.name}",
            details=(f"{inventory.fmt_qty(on_hand)} on hand against a minimum of "
                     f"{inventory.fmt_qty(minimum)}. "
                     + (f"Preferred supplier: {level.item.preferred_supplier.name}."
                        if level.item.preferred_supplier else
                        "No preferred supplier set.")),
            due_date=today,
        )
        created += is_new

    resolved = _clear_resolved(ReminderSource.LOW_STOCK, LOW_STOCK, live)
    return created, resolved


def overdue_purchase_reminders(warehouse=None):
    """One reminder per open purchase order whose ETA has passed."""
    today = datetime.date.today()
    created = 0
    live = set()

    for order in purchasing.overdue_orders(warehouse=warehouse):
        live.add(order.id)
        days = (today - order.expected_date).days
        _, is_new = _upsert(
            ReminderSource.PO_OVERDUE, PURCHASE_ORDER, order.id,
            title=f"{order.number} from {order.supplier.name} is {days} day(s) late",
            details=(f"Expected {order.expected_date.strftime('%d %b %Y')} at "
                     f"{order.warehouse.name}. Chase the supplier or revise the date."),
            due_date=order.expected_date,
        )
        created += is_new

    resolved = _clear_resolved(ReminderSource.PO_OVERDUE, PURCHASE_ORDER, live)
    return created, resolved


def expiring_lot_reminders(within_days=EXPIRY_HORIZON_DAYS, warehouse=None):
    """One reminder per batch expiring inside the horizon while it still holds stock."""
    today = datetime.date.today()
    created = 0
    live = set()

    for lot_stock in inventory.expiring_lots(within_days=within_days,
                                             warehouse=warehouse):
        lot = lot_stock.lot
        live.add(lot.id)
        days = (lot.expiry_date - today).days
        when = ("has expired" if days < 0
                else "expires today" if days == 0
                else f"expires in {days} day(s)")
        _, is_new = _upsert(
            ReminderSource.LOT_EXPIRY, LOT, lot.id,
            title=f"Batch {lot.lot_number} of {lot.item.name} {when}",
            details=(f"{inventory.fmt_qty(lot_stock.quantity)} still on hand at "
                     f"{lot_stock.warehouse.name}. Expiry "
                     f"{lot.expiry_date.strftime('%d %b %Y')}. "
                     f"Ship it first or write it off."),
            due_date=lot.expiry_date,
        )
        created += is_new

    resolved = _clear_resolved(ReminderSource.LOT_EXPIRY, LOT, live)
    return created, resolved


# --- Entry point --------------------------------------------------------------------

def refresh_all(warehouse=None) -> dict:
    """Regenerate every automatic reminder. Safe to call on each app start."""
    summary = {"created": 0, "resolved": 0}
    with db.atomic():
        for generator in (low_stock_reminders, overdue_purchase_reminders,
                          expiring_lot_reminders):
            try:
                created, resolved = generator(warehouse=warehouse)
                summary["created"] += created
                summary["resolved"] += resolved
            except Exception:
                # An alert failing must never stop the app opening.
                log.exception("Alert generator %s failed", generator.__name__)
    return summary


def active_reminders(limit=None, include_done=False):
    """Reminders for the Home panel: overdue first, then by due date."""
    query = Reminder.select()
    if not include_done:
        query = query.where(Reminder.is_done == False)  # noqa: E712
    query = query.order_by(Reminder.due_date.asc(), Reminder.id.asc())
    if limit:
        query = query.limit(limit)
    return list(query)


def reminder_counts() -> dict:
    """How many open reminders of each kind — drives the Home panel's badges."""
    counts = {source: 0 for source in ReminderSource.LABELS}
    for reminder in Reminder.select(Reminder.source).where(
        Reminder.is_done == False  # noqa: E712
    ):
        counts[reminder.source] = counts.get(reminder.source, 0) + 1
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts
