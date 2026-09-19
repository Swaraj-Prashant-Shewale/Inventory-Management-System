"""W3 analytics dashboard using the shared aggregate-query service."""
from decimal import Decimal

from fastapi import APIRouter, Request

from database.models import Warehouse
from services import analytics, auth
from web.context import forbidden, render, require_login, resolve_user

router = APIRouter()


def _change(current, previous):
    current = Decimal(str(current or 0))
    previous = Decimal(str(previous or 0))
    if previous == 0:
        return None
    return (current - previous) / previous * 100


@router.get("/analytics")
def analytics_dashboard(request: Request, period: str = analytics.THIS_FY,
                        warehouse: int = 0):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_VIEW_ANALYTICS):
        return forbidden(context, "view analytics")

    valid_periods = {value for value, _ in analytics.PERIOD_CHOICES}
    if period not in valid_periods:
        period = analytics.THIS_FY
    warehouses = list(Warehouse.select().where(
        Warehouse.is_active == True).order_by(Warehouse.name))  # noqa: E712
    selected = next((record for record in warehouses if record.id == warehouse), None)
    start, end, period_label = analytics.resolve_period(period)

    headline = analytics.headline(start, end, selected)
    purchases = analytics.purchasing_totals(start, end, selected)
    stock = analytics.stock_summary(selected)
    previous_start, previous_end = analytics.previous_period(start, end)
    previous = (analytics.headline(previous_start, previous_end, selected)
                if previous_start is not None else None)
    changes = {
        key: _change(headline[key], previous[key]) if previous else None
        for key in ("net_sales", "gross_profit", "items_sold", "transactions")
    }

    order_axis, order_values = analytics.monthly_orders(start, end, selected)
    inventory_axis, received_values, shipped_values = analytics.inventory_trend(
        start, end, selected)
    trend_rows = []
    for index, (key, label) in enumerate(inventory_axis):
        order_count = order_values[index] if index < len(order_values) else 0
        trend_rows.append({
            "key": key, "label": label, "orders": order_count,
            "received": received_values[index], "shipped": shipped_values[index],
        })

    top_items = analytics.top_items_by_quantity(start, end, limit=8,
                                                 warehouse=selected)
    profitable_items = analytics.top_items_by_profit(start, end, limit=8,
                                                      warehouse=selected)
    customers = analytics.top_customers(start, end, limit=8, warehouse=selected)
    delays = analytics.supplier_delays(limit=10, warehouse=selected)
    reliability = analytics.supplier_reliability(start, end, limit=8)

    maxima = {
        "items": max((entry["value"] for entry in top_items), default=Decimal("1")),
        "profit": max((entry["value"] for entry in profitable_items),
                      default=Decimal("1")),
        "customers": max((entry["value"] for entry in customers),
                         default=Decimal("1")),
    }
    for key, value in list(maxima.items()):
        if value <= 0:
            maxima[key] = Decimal("1")

    return render(
        context, "analytics.html", period=period, period_label=period_label,
        period_choices=analytics.PERIOD_CHOICES, warehouses=warehouses,
        selected=selected, headline=headline, purchases=purchases, stock=stock,
        changes=changes, trend_rows=trend_rows, top_items=top_items,
        profitable_items=profitable_items, customers=customers, delays=delays,
        reliability=reliability, maxima=maxima,
    )
