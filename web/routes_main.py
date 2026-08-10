"""The signed-in screens: dashboard and inventory. More arrive in web phase W2."""
import logging
import time
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Request
from peewee import JOIN

from database.connection import db

from database.models import (
    Customer,
    Item,
    PurchaseOrder,
    PurchaseStatus,
    SalesOrder,
    SalesStatus,
    StockLevel,
    Supplier,
    Uom,
    Warehouse,
)
from services import alerts, auth, inventory
from web.context import render, require_login, resolve_user

log = logging.getLogger(__name__)
router = APIRouter()

D = Decimal

# Regenerating every automatic reminder scans stock, POs and lots — too heavy to run on
# each Home load. Throttle to once per tenant per interval. Keyed by the active schema so
# tenants don't share a clock; single-process (documented --workers 1) so a plain dict is
# safe. A stale reminder for at most this long is fine — it refreshes on the next visit.
_ALERT_INTERVAL_SECONDS = 120
_last_alert_refresh = {}


def _maybe_refresh_alerts():
    key = db.active_schema() if hasattr(db, "active_schema") else "default"
    now = time.monotonic()
    last = _last_alert_refresh.get(key, 0)
    if now - last < _ALERT_INTERVAL_SECONDS:
        return
    _last_alert_refresh[key] = now
    try:
        alerts.refresh_all()
    except Exception:
        log.exception("Alert refresh failed")


@router.get("/")
def home(request: Request):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return blocked

    _maybe_refresh_alerts()

    reminders = alerts.active_reminders(limit=12)
    counts = {
        "items": Item.select().where(Item.is_active == True).count(),  # noqa: E712
        "open_purchases": PurchaseOrder.select().where(
            PurchaseOrder.status.in_(PurchaseStatus.OPEN)).count(),
        "open_sales": SalesOrder.select().where(
            SalesOrder.status.in_(SalesStatus.OPEN)).count(),
        "suppliers": Supplier.select().where(Supplier.is_active == True).count(),  # noqa: E712
        "customers": Customer.select().where(Customer.is_active == True).count(),  # noqa: E712
    }
    stock = inventory.total_stock_value() if context.can(auth.PERM_VIEW_COST) else None

    import datetime
    return render(context, "home.html", reminders=reminders, counts=counts,
                  stock_value=stock, now_date=datetime.date.today())


@router.get("/inventory")
def inventory_list(request: Request, q: str = "", warehouse: int = 0,
                   low: int = 0, inactive: int = 0):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return blocked

    warehouses = list(Warehouse.select().where(
        Warehouse.is_active == True).order_by(Warehouse.name))  # noqa: E712
    selected = next((w for w in warehouses if w.id == warehouse), None)

    query = (Item.select(Item, Supplier, Uom)
             .join(Supplier, JOIN.LEFT_OUTER, on=Item.preferred_supplier)
             .switch(Item)
             .join(Uom, JOIN.LEFT_OUTER, on=Item.base_uom))
    if not inactive:
        query = query.where(Item.is_active == True)  # noqa: E712
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.where((Item.name ** like) | (Item.sku ** like)
                            | (Item.barcode ** like))
    items = list(query.order_by(Item.name))

    # One query for every stock level, exactly as the desktop screen does.
    levels_by_item = defaultdict(list)
    if items:
        level_query = (StockLevel.select(StockLevel, Warehouse)
                       .join(Warehouse)
                       .where(StockLevel.item.in_(items)))
        if selected is not None:
            level_query = level_query.where(StockLevel.warehouse == selected)
        for level in level_query:
            levels_by_item[level.item_id].append(level)

    show_cost = context.can(auth.PERM_VIEW_COST)
    rows = []
    total_value = D(0)
    low_count = 0
    for item in items:
        levels = levels_by_item.get(item.id, [])
        on_hand = sum((D(str(l.on_hand or 0)) for l in levels), D(0))
        reserved = sum((D(str(l.reserved or 0)) for l in levels), D(0))

        if selected is not None and levels:
            level = levels[0]
            minimum = D(str(level.min_level if level.min_level is not None
                            else item.default_min_level or 0))
            location = selected.name
        else:
            minimum = sum(
                (D(str(l.min_level if l.min_level is not None
                       else item.default_min_level or 0)) for l in levels), D(0),
            ) or D(str(item.default_min_level or 0))
            stocked = [l for l in levels if D(str(l.on_hand or 0)) > 0]
            location = (stocked[0].warehouse.name if len(stocked) == 1
                        else f"{len(stocked)} warehouses" if stocked else "—")

        is_low = minimum > 0 and on_hand <= minimum
        if low and not is_low:
            continue
        low_count += is_low
        value = on_hand * D(str(item.avg_cost or 0))
        total_value += value

        rows.append({
            "item": item,
            "on_hand": on_hand,
            "available": on_hand - reserved,
            "minimum": minimum,
            "low": is_low,
            "value": value,
            "location": location,
        })

    return render(context, "inventory.html",
                  rows=rows, warehouses=warehouses, selected=selected,
                  q=term, low=low, inactive=inactive,
                  show_cost=show_cost, total_value=total_value,
                  low_count=low_count)


COMING = {
    "shipping": ("Shipping — Exports",
                 "Sales orders, fulfilment, GST invoices and payments arrive on the "
                 "web in phase W2. The business engine already runs them — only these "
                 "pages are pending."),
    "receiving": ("Receiving — Imports",
                  "Purchase orders and goods receipts arrive on the web in phase W2."),
    "logs": ("Recent Logs",
             "The stock movement trail arrives on the web in phase W2. Every movement "
             "is already being recorded."),
    "analytics": ("Analytics",
                  "Sales, profit and trend dashboards arrive on the web in phase W3."),
    "settings": ("Settings",
                 "Master data management on the web arrives in phase W2."),
}


@router.get("/coming/{section}")
def coming_soon(request: Request, section: str):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return blocked
    title, message = COMING.get(section, ("Not here yet", "This page is planned."))
    return render(context, "coming_soon.html", title=title, message=message)
