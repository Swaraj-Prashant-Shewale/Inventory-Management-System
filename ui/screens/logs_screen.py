"""Recent Logs — the stock movement trail.

Every receipt, shipment, transfer, adjustment, count and return already writes a row
through the single write path in services.inventory. This screen reads it back with
filters; it never computes anything of its own.
"""
import datetime

from peewee import JOIN
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from database.models import (
    Customer,
    Direction,
    DocType,
    Employee,
    Item,
    Lot,
    StockMovement,
    Supplier,
    User,
    Warehouse,
)
from ui import theme
from ui.widgets import common
from ui.widgets.table import (
    CENTER,
    Cell,
    Column,
    DataTable,
    RIGHT,
    Row,
    qty_cell,
    text_cell,
)

ALL = "__all__"
PAGE_SIZE = 500


class LogsScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user
        self._search = ""
        self._limit = PAGE_SIZE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        layout.addLayout(self._build_toolbar())
        layout.addLayout(self._build_filters())

        self.table = DataTable(
            [
                Column("Employee", width="content"),
                Column("In/Out", width=90, align=CENTER,
                       tooltip="Goods coming in or going out"),
                Column("Time", width=150, align=CENTER),
                Column("Item", width="stretch"),
                Column("Quantity", width=110, align=RIGHT),
                Column("Balance", width=110, align=RIGHT,
                       tooltip="Stock on hand immediately after this movement"),
                Column("Warehouse", width="content"),
                Column("Document", width=160),
                # The PDF calls this "Supplier", but outbound rows carry a customer.
                Column("Supplier / Customer", width="content"),
                Column("Truck HSRP", width=130, align=CENTER),
            ],
            checkable=False,
            empty_text="No stock movements match these filters.",
        )
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtle")
        layout.addWidget(self.status_label)

        self._load_filters()
        self.refresh()

    # --- Construction ---------------------------------------------------------------

    def _build_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(common.screen_title("RECENT LOGS"))

        refresh = common.IconButton("refresh", 34)
        refresh.setToolTip("Reload from the database")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        row.addStretch()

        self.more_button = common.action_button(
            "Load more", self._load_more, "action",
            tooltip=f"Movements load {PAGE_SIZE} at a time, newest first.")
        self.more_button.setEnabled(False)
        row.addWidget(self.more_button)
        row.addWidget(common.action_button("Export", self._export, "action"))
        return row

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        row.addWidget(common.field_label("Warehouse"))
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(160)
        self.warehouse_combo.currentIndexChanged.connect(self._reset_and_refresh)
        row.addWidget(self.warehouse_combo)

        row.addWidget(common.field_label("Type"))
        self.type_combo = common.ComboField()
        self.type_combo.setMinimumWidth(160)
        self.type_combo.load_choices(
            [(ALL, "All movements"), (Direction.IN, "Goods in only"),
             (Direction.OUT, "Goods out only")]
            + [(key, label) for key, label in DocType.LABELS.items()]
        )
        self.type_combo.currentIndexChanged.connect(self._reset_and_refresh)
        row.addWidget(self.type_combo)

        row.addWidget(common.field_label("From"))
        self.from_input = QDateEdit()
        self.from_input.setCalendarPopup(True)
        self.from_input.setDisplayFormat("dd MMM yyyy")
        self.from_input.setDate(QDate.currentDate().addDays(-30))
        self.from_input.dateChanged.connect(self._reset_and_refresh)
        row.addWidget(self.from_input)

        row.addWidget(common.field_label("To"))
        self.to_input = QDateEdit()
        self.to_input.setCalendarPopup(True)
        self.to_input.setDisplayFormat("dd MMM yyyy")
        self.to_input.setDate(QDate.currentDate())
        self.to_input.dateChanged.connect(self._reset_and_refresh)
        row.addWidget(self.to_input)

        self.all_dates_check = common.checkbox("All dates")
        self.all_dates_check.toggled.connect(self._on_all_dates)
        row.addWidget(self.all_dates_check)

        row.addStretch()
        return row

    def _load_filters(self):
        self.warehouse_combo.blockSignals(True)
        self.warehouse_combo.clear()
        self.warehouse_combo.addItem("All warehouses", ALL)
        for warehouse in Warehouse.select().where(
            Warehouse.is_active == True  # noqa: E712
        ).order_by(Warehouse.name):
            self.warehouse_combo.addItem(warehouse.name, warehouse)
        if self.user.warehouse is not None:
            self.warehouse_combo.select_record(self.user.warehouse)
        self.warehouse_combo.blockSignals(False)

    def _on_all_dates(self, checked):
        self.from_input.setEnabled(not checked)
        self.to_input.setEnabled(not checked)
        self._reset_and_refresh()

    def _reset_and_refresh(self):
        self._limit = PAGE_SIZE
        self.refresh()

    def _load_more(self):
        self._limit += PAGE_SIZE
        self.refresh()

    def set_search_text(self, text):
        self._search = (text or "").strip().lower()
        self._limit = PAGE_SIZE
        self.refresh()

    # --- Data -----------------------------------------------------------------------

    def refresh(self):
        if self.warehouse_combo.count() <= 1:
            self._load_filters()

        # Joined so each row costs no follow-up queries.
        query = (
            StockMovement
            .select(StockMovement, Item, Warehouse, Employee, User, Supplier, Customer,
                    Lot)
            .join(Item).switch(StockMovement)
            .join(Warehouse).switch(StockMovement)
            .join(Employee, JOIN.LEFT_OUTER).switch(StockMovement)
            .join(User, JOIN.LEFT_OUTER).switch(StockMovement)
            .join(Supplier, JOIN.LEFT_OUTER).switch(StockMovement)
            .join(Customer, JOIN.LEFT_OUTER).switch(StockMovement)
            .join(Lot, JOIN.LEFT_OUTER)
        )

        warehouse = self.warehouse_combo.currentData()
        if warehouse not in (None, ALL):
            query = query.where(StockMovement.warehouse == warehouse)

        kind = self.type_combo.current()
        if kind in (Direction.IN, Direction.OUT):
            query = query.where(StockMovement.direction == kind)
        elif kind not in (None, ALL):
            query = query.where(StockMovement.doc_type == kind)

        if not self.all_dates_check.isChecked():
            start = self.from_input.date().toPython()
            end = self.to_input.date().toPython()
            query = query.where(
                (StockMovement.timestamp >= datetime.datetime.combine(
                    start, datetime.time.min))
                & (StockMovement.timestamp <= datetime.datetime.combine(
                    end, datetime.time.max))
            )

        if self._search:
            term = f"%{self._search}%"
            query = query.where(
                (Item.name ** term) | (Item.sku ** term)
                | (StockMovement.doc_number ** term)
                | (StockMovement.truck_hsrp ** term)
            )

        total = query.count()
        movements = list(query.order_by(StockMovement.timestamp.desc(),
                                        StockMovement.id.desc()).limit(self._limit))

        rows = []
        units_in = units_out = 0
        for movement in movements:
            is_in = movement.direction == Direction.IN
            units_in += float(movement.quantity or 0) if is_in else 0
            units_out += float(movement.quantity or 0) if not is_in else 0

            who = (movement.employee.name if movement.employee
                   else movement.user.full_name if movement.user else "—")
            partner = (movement.supplier.name if movement.supplier
                       else movement.customer.name if movement.customer else "—")

            document = movement.doc_number or DocType.LABELS.get(movement.doc_type,
                                                                 movement.doc_type)
            item_label = movement.item.name
            if movement.lot is not None:
                item_label += f"  ·  batch {movement.lot.lot_number}"

            rows.append(Row(
                cells=[
                    text_cell(who),
                    Cell("In" if is_in else "Out", align=CENTER,
                         colour=theme.SUCCESS if is_in else theme.DANGER, bold=True,
                         sort_value=movement.direction),
                    Cell(theme.datetime_short(movement.timestamp), align=CENTER,
                         sort_value=movement.timestamp),
                    text_cell(item_label,
                              tooltip=f"{movement.item.sku} — {movement.item.name}"),
                    qty_cell(movement.quantity,
                             tone=theme.SUCCESS if is_in else theme.DANGER),
                    qty_cell(movement.balance_after, tone=theme.TEXT_MUTED),
                    text_cell(movement.warehouse.name),
                    text_cell(document,
                              tooltip=DocType.LABELS.get(movement.doc_type,
                                                         movement.doc_type)),
                    text_cell(partner),
                    text_cell(movement.truck_hsrp or "—", align=CENTER),
                ],
                payload=movement,
            ))

        self.table.set_rows(rows)
        self.more_button.setEnabled(len(movements) < total)
        shown = f"{len(rows)} of {total}" if total > len(rows) else f"{total}"
        self.status_label.setText(
            f"{shown} movement(s)  ·  {units_in:,.0f} units in  ·  "
            f"{units_out:,.0f} units out"
        )

    # --- Actions --------------------------------------------------------------------

    def _export(self):
        if not self.table.row_count():
            common.info(self, "Nothing to export", "There are no rows in the table.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export movement log", "stock_movements",
            "Excel workbook (*.xlsx);;CSV file (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                self.table.export_csv(path if path.lower().endswith(".csv")
                                      else path + ".csv", title="Stock Movements")
            else:
                self.table.export_xlsx(path if path.lower().endswith(".xlsx")
                                       else path + ".xlsx", title="Stock Movements")
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))

    def _context_menu(self, position):
        movement = self.table.current_payload()
        if movement is None:
            return
        menu = QMenu(self)
        detail = menu.addAction("Movement details…")
        filter_item = menu.addAction(f"Show only {movement.item.name}")
        menu.addSeparator()
        copy = menu.addAction("Copy row")

        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is detail:
            self._show_detail(movement)
        elif chosen is filter_item:
            self.set_search_text(movement.item.sku)
        elif chosen is copy:
            self.table.copy_selection()

    def _show_detail(self, movement):
        lines = [
            f"Item        {movement.item.sku} — {movement.item.name}",
            f"Direction   {'In' if movement.direction == Direction.IN else 'Out'}",
            f"Quantity    {theme.quantity(movement.quantity)}",
            f"Balance     {theme.quantity(movement.balance_after)} after this movement",
            f"Unit cost   {theme.money(movement.unit_cost)}",
            f"Warehouse   {movement.warehouse.name}",
            f"Document    {movement.doc_number or '—'} "
            f"({DocType.LABELS.get(movement.doc_type, movement.doc_type)})",
            f"When        {theme.datetime_short(movement.timestamp)}",
        ]
        if movement.lot is not None:
            lines.append(f"Batch       {movement.lot.lot_number}"
                         + (f", expires {theme.date_short(movement.lot.expiry_date)}"
                            if movement.lot.expiry_date else ""))
        if movement.user is not None:
            lines.append(f"Recorded by {movement.user.full_name}")
        if movement.truck_hsrp:
            lines.append(f"Truck       {movement.truck_hsrp}")
        if movement.notes:
            lines.append(f"Notes       {movement.notes}")

        common.info(self, "Movement detail",
                    f"{movement.item.name} — "
                    f"{DocType.LABELS.get(movement.doc_type, movement.doc_type)}",
                    "\n".join(lines))
