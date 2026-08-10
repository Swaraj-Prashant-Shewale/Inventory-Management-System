"""Reporting metrics for the Analytics dashboard.

Revenue books when goods ship, so every sales figure reads from FulfillmentLine rather
than from order headers. Cost of goods sold is the unit_cost frozen onto that line at
ship time, which is why historic profit never moves when prices change later.

Everything here is written as an aggregate query. Round trips are the binding constraint
against a remote database — see the performance note in the README.
"""
import calendar
import datetime
from decimal import Decimal

from peewee import JOIN, fn

import config
from database.models import (
    ZERO,
    Customer,
    Fulfillment,
    FulfillmentLine,
    GoodsReceipt,
    GoodsReceiptLine,
    Item,
    PurchaseOrder,
    PurchaseStatus,
    SalesOrder,
    SalesStatus,
    StockLevel,
    Supplier,
)


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


# --- Periods ------------------------------------------------------------------------

THIS_MONTH = "this_month"
LAST_MONTH = "last_month"
LAST_3 = "last_3_months"
LAST_6 = "last_6_months"
LAST_12 = "last_12_months"
THIS_FY = "this_fy"
ALL_TIME = "all_time"

PERIOD_CHOICES = [
    (THIS_MONTH, "This month"),
    (LAST_MONTH, "Last month"),
    (LAST_3, "Last 3 months"),
    (LAST_6, "Last 6 months"),
    (LAST_12, "Last 12 months"),
    (THIS_FY, "This financial year"),
    (ALL_TIME, "All time"),
]


def _month_start(date):
    return date.replace(day=1)


def _month_end(date):
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


def _add_months(date, months):
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def resolve_period(period, today=None):
    """Return (start_date, end_date, label). `start` is None for all time."""
    today = today or datetime.date.today()

    if period == THIS_MONTH:
        return _month_start(today), _month_end(today), today.strftime("%B %Y")
    if period == LAST_MONTH:
        previous = _add_months(_month_start(today), -1)
        return _month_start(previous), _month_end(previous), previous.strftime("%B %Y")
    if period in (LAST_3, LAST_6, LAST_12):
        months = {LAST_3: 3, LAST_6: 6, LAST_12: 12}[period]
        start = _month_start(_add_months(today, -(months - 1)))
        return start, today, f"{start.strftime('%b %Y')} – {today.strftime('%b %Y')}"
    if period == THIS_FY:
        fy_month = config.FINANCIAL_YEAR_START_MONTH
        year = today.year if today.month >= fy_month else today.year - 1
        start = datetime.date(year, fy_month, 1)
        return start, today, f"FY {year % 100:02d}-{(year + 1) % 100:02d}"
    return None, today, "All time"


def previous_period(start, end):
    """The immediately preceding window of equal length, for period-on-period deltas."""
    if start is None:
        return None, None
    span = (end - start).days + 1
    previous_end = start - datetime.timedelta(days=1)
    return previous_end - datetime.timedelta(days=span - 1), previous_end


# --- Headline metrics ---------------------------------------------------------------

def _fulfillment_window(query, start, end, warehouse=None):
    if start is not None:
        query = query.where(Fulfillment.ship_date >= start)
    if end is not None:
        query = query.where(Fulfillment.ship_date <= end)
    if warehouse is not None:
        query = query.where(Fulfillment.warehouse == warehouse)
    return query


def headline(start, end, warehouse=None) -> dict:
    """Net sales, COGS, gross profit, items sold and transaction count in one query."""
    revenue = fn.SUM(FulfillmentLine.quantity * FulfillmentLine.unit_price)
    cogs = fn.SUM(FulfillmentLine.quantity * FulfillmentLine.unit_cost)

    query = _fulfillment_window(
        FulfillmentLine.select(
            fn.COALESCE(revenue, 0),
            fn.COALESCE(cogs, 0),
            fn.COALESCE(fn.SUM(FulfillmentLine.quantity), 0),
            fn.COUNT(fn.DISTINCT(Fulfillment.order)),
            fn.COUNT(fn.DISTINCT(Fulfillment.id)),
        ).join(Fulfillment),
        start, end, warehouse,
    )
    row = query.tuples().first() or (0, 0, 0, 0, 0)
    net_sales, cost_of_sales, items_sold, orders, shipments = (
        _dec(row[0]), _dec(row[1]), _dec(row[2]), row[3] or 0, row[4] or 0,
    )

    return {
        "net_sales": net_sales,
        "total_costs": cost_of_sales,
        "gross_profit": net_sales - cost_of_sales,
        "margin_percent": ((net_sales - cost_of_sales) / net_sales * 100)
        if net_sales else ZERO,
        "items_sold": items_sold,
        "transactions": orders,
        "shipments": shipments,
    }


def purchasing_totals(start, end, warehouse=None) -> dict:
    """What was spent bringing goods in, over the same window."""
    query = GoodsReceiptLine.select(
        fn.COALESCE(fn.SUM(GoodsReceiptLine.quantity * GoodsReceiptLine.unit_cost), 0),
        fn.COALESCE(fn.SUM(GoodsReceiptLine.quantity), 0),
        fn.COUNT(fn.DISTINCT(GoodsReceipt.id)),
    ).join(GoodsReceipt)
    if start is not None:
        query = query.where(GoodsReceipt.receipt_date >= start)
    if end is not None:
        query = query.where(GoodsReceipt.receipt_date <= end)
    if warehouse is not None:
        query = query.where(GoodsReceipt.warehouse == warehouse)

    row = query.tuples().first() or (0, 0, 0)
    return {"purchase_value": _dec(row[0]), "units_received": _dec(row[1]),
            "receipts": row[2] or 0}


# --- Trends -------------------------------------------------------------------------

def _month_key(value):
    return value.strftime("%Y-%m") if value else None


def _month_axis(start, end, months=None):
    """Ordered list of (key, short label) covering the window."""
    if start is None:
        start = _month_start(_add_months(end, -(months or 12) + 1))
    cursor = _month_start(start)
    axis = []
    while cursor <= end:
        axis.append((cursor.strftime("%Y-%m"), cursor.strftime("%b")))
        cursor = _add_months(cursor, 1)
        if len(axis) > 36:
            break
    return axis


def monthly_orders(start, end, warehouse=None):
    """Sales orders raised per month — the Monthly Orders Trend chart."""
    axis = _month_axis(start, end)
    counts = {key: 0 for key, _ in axis}

    query = SalesOrder.select(SalesOrder.order_date).where(
        SalesOrder.status != SalesStatus.CANCELLED)
    if start is not None:
        query = query.where(SalesOrder.order_date >= start)
    query = query.where(SalesOrder.order_date <= end)
    if warehouse is not None:
        query = query.where(SalesOrder.warehouse == warehouse)

    for row in query.tuples():
        key = _month_key(row[0])
        if key in counts:
            counts[key] += 1
    return axis, [counts[key] for key, _ in axis]


def inventory_trend(start, end, warehouse=None):
    """Units received vs units shipped per month — the Inventory Trend chart."""
    axis = _month_axis(start, end)
    imports = {key: ZERO for key, _ in axis}
    exports = {key: ZERO for key, _ in axis}

    receipts = (GoodsReceiptLine
                .select(GoodsReceipt.receipt_date, GoodsReceiptLine.quantity)
                .join(GoodsReceipt))
    if start is not None:
        receipts = receipts.where(GoodsReceipt.receipt_date >= start)
    receipts = receipts.where(GoodsReceipt.receipt_date <= end)
    if warehouse is not None:
        receipts = receipts.where(GoodsReceipt.warehouse == warehouse)
    for date, quantity in receipts.tuples():
        key = _month_key(date)
        if key in imports:
            imports[key] += _dec(quantity)

    shipments = (FulfillmentLine
                 .select(Fulfillment.ship_date, FulfillmentLine.quantity)
                 .join(Fulfillment))
    shipments = _fulfillment_window(shipments, start, end, warehouse)
    for date, quantity in shipments.tuples():
        key = _month_key(date)
        if key in exports:
            exports[key] += _dec(quantity)

    return axis, [imports[k] for k, _ in axis], [exports[k] for k, _ in axis]


def price_trend(start, end, item_limit=3, warehouse=None):
    """Average realised selling price per month for the best-selling items."""
    top = top_items_by_quantity(start, end, limit=item_limit, warehouse=warehouse)
    if not top:
        return _month_axis(start, end), []

    axis = _month_axis(start, end)
    item_ids = [entry["item"].id for entry in top]

    query = (FulfillmentLine
             .select(Fulfillment.ship_date, FulfillmentLine.item,
                     FulfillmentLine.quantity, FulfillmentLine.unit_price)
             .join(Fulfillment)
             .where(FulfillmentLine.item.in_(item_ids)))
    query = _fulfillment_window(query, start, end, warehouse)

    totals = {item_id: {key: [ZERO, ZERO] for key, _ in axis} for item_id in item_ids}
    for date, item_id, quantity, price in query.tuples():
        key = _month_key(date)
        bucket = totals.get(item_id, {}).get(key)
        if bucket is not None:
            bucket[0] += _dec(quantity) * _dec(price)
            bucket[1] += _dec(quantity)

    series = []
    for entry in top:
        item = entry["item"]
        values = []
        for key, _ in axis:
            revenue, units = totals[item.id][key]
            values.append(revenue / units if units else None)
        series.append({"item": item, "values": values})
    return axis, series


# --- Rankings -----------------------------------------------------------------------

def top_items_by_quantity(start, end, limit=4, warehouse=None):
    """Most exported items, by units shipped."""
    query = (FulfillmentLine
             .select(FulfillmentLine.item,
                     fn.SUM(FulfillmentLine.quantity).alias("units"))
             .join(Fulfillment))
    query = _fulfillment_window(query, start, end, warehouse)
    rows = (query.group_by(FulfillmentLine.item)
            .order_by(fn.SUM(FulfillmentLine.quantity).desc())
            .limit(limit).tuples())

    results = []
    item_ids = [row[0] for row in rows]
    if not item_ids:
        return results
    items = {i.id: i for i in Item.select().where(Item.id.in_(item_ids))}
    for item_id, units in rows:
        item = items.get(item_id)
        if item is not None:
            results.append({"item": item, "value": _dec(units)})
    return results


def top_items_by_profit(start, end, limit=4, warehouse=None):
    """Highest-profit items: shipped revenue minus the cost frozen at ship time."""
    profit = fn.SUM(
        FulfillmentLine.quantity * (FulfillmentLine.unit_price - FulfillmentLine.unit_cost)
    )
    query = (FulfillmentLine
             .select(FulfillmentLine.item, profit.alias("profit"))
             .join(Fulfillment))
    query = _fulfillment_window(query, start, end, warehouse)
    rows = list(query.group_by(FulfillmentLine.item)
                .order_by(profit.desc()).limit(limit).tuples())

    item_ids = [row[0] for row in rows]
    if not item_ids:
        return []
    items = {i.id: i for i in Item.select().where(Item.id.in_(item_ids))}
    return [{"item": items[item_id], "value": _dec(value)}
            for item_id, value in rows
            if item_id in items and _dec(value) > 0]


def top_customers(start, end, limit=5, warehouse=None):
    revenue = fn.SUM(FulfillmentLine.quantity * FulfillmentLine.unit_price)
    query = (FulfillmentLine
             .select(SalesOrder.customer, revenue.alias("revenue"))
             .join(Fulfillment)
             .join(SalesOrder, on=(Fulfillment.order == SalesOrder.id)))
    query = _fulfillment_window(query, start, end, warehouse)
    rows = list(query.group_by(SalesOrder.customer)
                .order_by(revenue.desc()).limit(limit).tuples())
    if not rows:
        return []
    customers = {c.id: c for c in Customer.select().where(
        Customer.id.in_([r[0] for r in rows]))}
    return [{"customer": customers[cid], "value": _dec(value)}
            for cid, value in rows if cid in customers]


# --- Supplier delays ----------------------------------------------------------------

def supplier_delays(limit=8, warehouse=None):
    """Open purchase orders past their expected date — the Delays panel."""
    today = datetime.date.today()
    query = (PurchaseOrder
             .select(PurchaseOrder, Supplier)
             .join(Supplier)
             .where((PurchaseOrder.status.in_(PurchaseStatus.OPEN))
                    & (PurchaseOrder.expected_date.is_null(False))
                    & (PurchaseOrder.expected_date < today)))
    if warehouse is not None:
        query = query.where(PurchaseOrder.warehouse == warehouse)

    return [{"order": order, "supplier": order.supplier,
             "expected": order.expected_date,
             "days_late": (today - order.expected_date).days}
            for order in query.order_by(PurchaseOrder.expected_date.asc()).limit(limit)]


def supplier_reliability(start, end, limit=8):
    """On-time delivery rate per supplier, from receipt date vs the promised date."""
    query = (GoodsReceipt
             .select(GoodsReceipt.supplier, GoodsReceipt.receipt_date,
                     PurchaseOrder.expected_date)
             .join(PurchaseOrder, JOIN.LEFT_OUTER)
             .where(GoodsReceipt.supplier.is_null(False)))
    if start is not None:
        query = query.where(GoodsReceipt.receipt_date >= start)
    query = query.where(GoodsReceipt.receipt_date <= end)

    stats = {}
    for supplier_id, received, expected in query.tuples():
        entry = stats.setdefault(supplier_id, {"total": 0, "late": 0, "days": 0})
        entry["total"] += 1
        if expected and received and received > expected:
            entry["late"] += 1
            entry["days"] += (received - expected).days

    if not stats:
        return []
    suppliers = {s.id: s for s in Supplier.select().where(
        Supplier.id.in_(list(stats)))}
    results = []
    for supplier_id, entry in stats.items():
        supplier = suppliers.get(supplier_id)
        if supplier is None:
            continue
        results.append({
            "supplier": supplier,
            "deliveries": entry["total"],
            "late": entry["late"],
            "on_time_percent": Decimal(entry["total"] - entry["late"])
            / Decimal(entry["total"]) * 100,
            "average_days_late": (Decimal(entry["days"]) / Decimal(entry["late"]))
            if entry["late"] else ZERO,
        })
    results.sort(key=lambda r: r["on_time_percent"])
    return results[:limit]


# --- Stock position -----------------------------------------------------------------

def stock_summary(warehouse=None) -> dict:
    """Current valuation and how much of the catalogue is short."""
    query = (StockLevel
             .select(fn.COALESCE(fn.SUM(StockLevel.on_hand * Item.avg_cost), 0),
                     fn.COALESCE(fn.SUM(StockLevel.on_hand), 0),
                     fn.COUNT(StockLevel.id))
             .join(Item))
    if warehouse is not None:
        query = query.where(StockLevel.warehouse == warehouse)
    row = query.tuples().first() or (0, 0, 0)
    return {"stock_value": _dec(row[0]), "units_on_hand": _dec(row[1]),
            "stocked_lines": row[2] or 0}
