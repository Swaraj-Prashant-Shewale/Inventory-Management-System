"""Analytics dashboard.

Every figure derives from goods that actually shipped, using the cost frozen onto the
fulfilment line at ship time — so the profit shown for a past month never moves when
today's purchase prices change.
"""
from decimal import Decimal

from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from database.models import Warehouse
from services import analytics, auth
from ui import theme
from ui.widgets import common
from ui.widgets.charts import (
    ChartCard,
    EmptyChart,
    StatTile,
    horizontal_bar_chart,
    line_chart,
)

ALL = "__all__"


class AnalyticsScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user
        self._built = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 12)
        outer.setSpacing(12)

        outer.addLayout(self._build_toolbar())

        if not auth.can(user, auth.PERM_VIEW_ANALYTICS):
            notice = common.Card(padding=24)
            notice.add(common.section_title("Not available for your role"))
            notice.add(common.subtle(
                "Analytics shows sales, margins and costs. Ask an Owner if you need "
                "access."))
            outer.addWidget(notice)
            outer.addStretch()
            return

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.body = None
        self.body_layout = None
        outer.addWidget(self.scroll, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtle")
        outer.addWidget(self.status_label)

        self._load_filters()
        self.refresh()

    # --- Filters ---------------------------------------------------------------------

    def _build_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(common.screen_title("ANALYTICS"))

        refresh = common.IconButton("refresh", 34)
        refresh.setToolTip("Recalculate from the database")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        row.addStretch()

        # One filter row above everything it scopes; no per-chart filters.
        row.addWidget(common.field_label("Period"))
        self.period_combo = common.ComboField()
        self.period_combo.setMinimumWidth(190)
        self.period_combo.load_choices(analytics.PERIOD_CHOICES,
                                       selected=analytics.LAST_6)
        self.period_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.period_combo)

        row.addWidget(common.field_label("Warehouse"))
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(180)
        self.warehouse_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.warehouse_combo)

        self.export_button = common.action_button("Export summary", self._export,
                                                  "action")
        row.addWidget(self.export_button)
        return row

    def _load_filters(self):
        self.warehouse_combo.blockSignals(True)
        self.warehouse_combo.clear()
        self.warehouse_combo.addItem("All warehouses", ALL)
        for warehouse in Warehouse.select().where(
            Warehouse.is_active == True  # noqa: E712
        ).order_by(Warehouse.name):
            self.warehouse_combo.addItem(warehouse.name, warehouse)
        self.warehouse_combo.blockSignals(False)

    def _current_warehouse(self):
        value = self.warehouse_combo.currentData()
        return None if value in (None, ALL) else value

    # --- Build -----------------------------------------------------------------------

    def refresh(self):
        """Rebuild the dashboard into a fresh body widget.

        Clearing the old layout in place would not work: deleteLater() is deferred, so
        the previous cards stay on screen and the new ones draw on top of them.
        Swapping the scroll area's widget destroys the old tree immediately.
        """
        if not auth.can(self.user, auth.PERM_VIEW_ANALYTICS):
            return
        if self.warehouse_combo.count() <= 1:
            self._load_filters()

        period = self.period_combo.current() or analytics.LAST_6
        start, end, label = analytics.resolve_period(period)
        warehouse = self._current_warehouse()

        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 8, 8)
        self.body_layout.setSpacing(14)

        self._build_kpis(start, end, warehouse)
        self._build_trend_row(start, end, warehouse)
        self._build_bottom_row(start, end, warehouse)
        self.body_layout.addStretch()

        previous = self.scroll.takeWidget()
        if previous is not None:
            previous.deleteLater()
        self.scroll.setWidget(body)
        self.body = body

        scope = warehouse.name if warehouse else "all warehouses"
        self.status_label.setText(
            f"{label}  ·  {scope}  ·  revenue and profit count goods once they ship"
        )

    def _build_kpis(self, start, end, warehouse):
        figures = analytics.headline(start, end, warehouse)
        previous_start, previous_end = analytics.previous_period(start, end)
        previous = (analytics.headline(previous_start, previous_end, warehouse)
                    if previous_start else None)

        def delta(key):
            if not previous or not previous.get(key):
                return None
            before = Decimal(str(previous[key]))
            if before == 0:
                return None
            change = (Decimal(str(figures[key])) - before) / before * 100
            return f"{'+' if change >= 0 else ''}{change:.0f}% vs previous"

        tiles = [
            ("NET SALES", theme.money(figures["net_sales"]), theme.CARD_NET_SALES,
             delta("net_sales")),
            ("GROSS PROFIT", theme.money(figures["gross_profit"]), theme.CARD_PROFIT,
             f"{figures['margin_percent']:.1f}% margin"),
            ("TOTAL TRANSACTIONS", f"{figures['transactions']:,}",
             theme.CARD_TRANSACTIONS, f"{figures['shipments']:,} shipments"),
            ("ITEMS SOLD", theme.quantity(figures["items_sold"]),
             theme.CARD_ITEMS_SOLD, delta("items_sold")),
            ("TOTAL COSTS", theme.money(figures["total_costs"]), theme.CARD_COSTS,
             "cost of goods shipped"),
        ]

        # Six columns so the PDF's proportions hold: two wide tiles above three.
        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (label, value, colour, subtitle) in enumerate(tiles):
            tile = StatTile(label, value, colour, subtitle)
            if index < 2:
                grid.addWidget(tile, 0, index * 3, 1, 3)
            else:
                grid.addWidget(tile, 1, (index - 2) * 2, 1, 2)
        for column in range(6):
            grid.setColumnStretch(column, 1)
        self.body_layout.addLayout(grid)

    def _build_trend_row(self, start, end, warehouse):
        row = QHBoxLayout()
        row.setSpacing(14)

        # --- Inventory trend: two series, so a legend is present ---------------------
        axis, imports, exports = analytics.inventory_trend(start, end, warehouse)
        card = common.Card(padding=16)
        if any(imports) or any(exports):
            chart = line_chart(axis, [
                {"name": "Imports", "values": imports},
                {"name": "Exports", "values": exports},
            ], value_formatter=lambda v: f"{v:,.0f} units")
            card.add(ChartCard("Inventory Trend", chart, height=250))
        else:
            card.add(common.section_title("Inventory Trend"))
            card.add(EmptyChart("No goods have moved in this period."))
        row.addWidget(card, 3)

        # --- Monthly orders: single series, so no legend box -------------------------
        axis, counts = analytics.monthly_orders(start, end, warehouse)
        orders_card = common.Card(padding=16)
        if any(counts):
            chart = line_chart(axis, [{"name": "Orders", "values": counts}],
                               value_formatter=lambda v: f"{v:,.0f} orders")
            orders_card.add(ChartCard("Monthly Orders Trend", chart, height=250))
        else:
            orders_card.add(common.section_title("Monthly Orders Trend"))
            orders_card.add(EmptyChart("No sales orders in this period."))
        row.addWidget(orders_card, 2)

        self.body_layout.addLayout(row)

    def _build_bottom_row(self, start, end, warehouse):
        row = QHBoxLayout()
        row.setSpacing(14)

        # --- Price trends: up to three items ----------------------------------------
        axis, series = analytics.price_trend(start, end, item_limit=3,
                                             warehouse=warehouse)
        price_card = common.Card(padding=16)
        if series:
            chart = line_chart(
                axis,
                [{"name": entry["item"].name, "values": entry["values"]}
                 for entry in series],
                value_formatter=lambda v: theme.money(v),
            )
            price_card.add(ChartCard("Price Trends", chart, height=240))
            price_card.add(common.subtle(
                "Average realised price per unit for the three best-selling items."))
        else:
            price_card.add(common.section_title("Price Trends"))
            price_card.add(EmptyChart("Nothing has shipped in this period yet."))
        row.addWidget(price_card, 3)

        right = QVBoxLayout()
        right.setSpacing(14)

        # --- Most exported items: ranked bars, one hue ------------------------------
        top = analytics.top_items_by_quantity(start, end, limit=4, warehouse=warehouse)
        exported_card = common.Card(padding=16)
        if top:
            chart = horizontal_bar_chart(
                [{"label": entry["item"].name, "value": entry["value"]}
                 for entry in top],
                value_formatter=lambda v: f"{v:,.0f} units",
            )
            exported_card.add(ChartCard("Most Exported Items", chart, height=190))
        else:
            exported_card.add(common.section_title("Most Exported Items"))
            exported_card.add(EmptyChart("Nothing shipped in this period."))
        right.addWidget(exported_card)

        # --- Delays: a list, not a chart ---------------------------------------------
        delays_card = common.Card(padding=16)
        delays_card.add(common.section_title("Delays"))
        delays = analytics.supplier_delays(limit=5, warehouse=warehouse)
        if delays:
            for entry in delays:
                item = QWidget()
                line = QVBoxLayout(item)
                line.setContentsMargins(10, 4, 4, 4)
                line.setSpacing(0)
                item.setStyleSheet(
                    f"background-color: #FFF6F5; border-radius: 6px;"
                    f" border-left: 4px solid {theme.DANGER};"
                )
                when = QLabel(f"{theme.date_short(entry['expected'])}  ·  "
                              f"{entry['days_late']} day(s) late")
                when.setStyleSheet(
                    f"color: {theme.DANGER}; font-size: 11px; font-weight: 700;"
                    f" background: transparent; border: none;")
                who = QLabel(f"{entry['supplier'].name} — {entry['order'].number}")
                who.setStyleSheet(
                    f"color: {theme.TEXT}; font-size: 12px; background: transparent;"
                    f" border: none;")
                line.addWidget(when)
                line.addWidget(who)
                delays_card.add(item)
        else:
            delays_card.add(common.subtle("No purchase orders are overdue."))
        right.addWidget(delays_card)
        right.addStretch()

        container = QWidget()
        container.setLayout(right)
        row.addWidget(container, 2)
        self.body_layout.addLayout(row)

    # --- Export ---------------------------------------------------------------------

    def _export(self):
        """Every chart has a readable equivalent — this is the table view."""
        period = self.period_combo.current() or analytics.LAST_6
        start, end, label = analytics.resolve_period(period)
        warehouse = self._current_warehouse()

        path, _ = QFileDialog.getSaveFileName(
            self, "Export analytics summary", "analytics_summary",
            "Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        try:
            self._write_workbook(path, start, end, label, warehouse)
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))

    def _write_workbook(self, path, start, end, label, warehouse):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        book = Workbook()
        fill = PatternFill("solid", fgColor="005F6B")

        def header(sheet, titles):
            sheet.append(titles)
            for column in range(1, len(titles) + 1):
                cell = sheet.cell(row=1, column=column)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = fill
                cell.alignment = Alignment(horizontal="center")

        figures = analytics.headline(start, end, warehouse)
        purchases = analytics.purchasing_totals(start, end, warehouse)
        stock = analytics.stock_summary(warehouse)

        sheet = book.active
        sheet.title = "Summary"
        header(sheet, ["Metric", "Value"])
        for name, value in (
            ("Period", label),
            ("Warehouse", warehouse.name if warehouse else "All warehouses"),
            ("Net sales", float(figures["net_sales"])),
            ("Cost of goods shipped", float(figures["total_costs"])),
            ("Gross profit", float(figures["gross_profit"])),
            ("Margin %", float(figures["margin_percent"])),
            ("Items sold", float(figures["items_sold"])),
            ("Transactions", figures["transactions"]),
            ("Shipments", figures["shipments"]),
            ("Purchases received (value)", float(purchases["purchase_value"])),
            ("Units received", float(purchases["units_received"])),
            ("Stock on hand (value)", float(stock["stock_value"])),
        ):
            sheet.append([name, value])
        sheet.column_dimensions["A"].width = 30
        sheet.column_dimensions["B"].width = 24

        trend = book.create_sheet("Inventory Trend")
        header(trend, ["Month", "Units In", "Units Out"])
        axis, imports, exports = analytics.inventory_trend(start, end, warehouse)
        for (key, month), units_in, units_out in zip(axis, imports, exports):
            trend.append([month, float(units_in), float(units_out)])

        orders = book.create_sheet("Monthly Orders")
        header(orders, ["Month", "Sales Orders"])
        axis, counts = analytics.monthly_orders(start, end, warehouse)
        for (key, month), count in zip(axis, counts):
            orders.append([month, count])

        items = book.create_sheet("Top Items")
        header(items, ["Item", "SKU", "Units Shipped", "Gross Profit"])
        by_quantity = analytics.top_items_by_quantity(start, end, limit=20,
                                                      warehouse=warehouse)
        profits = {entry["item"].id: entry["value"]
                   for entry in analytics.top_items_by_profit(start, end, limit=100,
                                                              warehouse=warehouse)}
        for entry in by_quantity:
            item = entry["item"]
            items.append([item.name, item.sku, float(entry["value"]),
                          float(profits.get(item.id, 0))])
        items.column_dimensions["A"].width = 34

        delays = book.create_sheet("Delays")
        header(delays, ["Order", "Supplier", "Expected", "Days Late"])
        for entry in analytics.supplier_delays(limit=100, warehouse=warehouse):
            delays.append([entry["order"].number, entry["supplier"].name,
                           entry["expected"].strftime("%d %b %Y"), entry["days_late"]])
        delays.column_dimensions["B"].width = 28

        book.save(path)
