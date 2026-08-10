"""Inventory screen — the item list with per-warehouse stock.

Matches the design's New Item / Delete / Order / Order Max toolbar, and adds the
warehouse selector that per-warehouse stock tracking requires.
"""
from collections import defaultdict
from decimal import Decimal

from peewee import JOIN
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from database.connection import db
from database.models import Category, Item, StockLevel, Supplier, Uom, Warehouse
from services import auth, documents, inventory, purchasing
from ui import theme
from ui.open_document import open_document
from ui.dialogs.item_dialog import ItemDialog
from ui.dialogs.reorder_dialog import ReorderDialog
from ui.widgets import common
from ui.widgets.table import (
    Column,
    DataTable,
    RIGHT,
    Row,
    money_cell,
    qty_cell,
    text_cell,
)

ALL_WAREHOUSES = "__all__"


class InventoryScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user
        self._search_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        layout.addLayout(self._build_toolbar())
        layout.addLayout(self._build_filters())

        self.table = DataTable(self._columns(), checkable=True,
                               empty_text="No items yet — press New Item to add your first.")
        self.table.check_changed.connect(self._update_button_states)
        self.table.selection_changed.connect(self._update_button_states)
        self.table.row_activated.connect(self._edit_item)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtle")
        layout.addWidget(self.status_label)

        self._load_warehouses()
        self.refresh()

    # --- Construction ---------------------------------------------------------------

    def _columns(self):
        columns = [
            Column("Product", width="stretch"),
            Column("SKU", width=120),
            Column("On Hand", width=110, align=RIGHT),
            Column("Available", width=110, align=RIGHT,
                   tooltip="On hand minus stock reserved for confirmed sales orders"),
            Column("Min", width=90, align=RIGHT),
            Column("Max", width=90, align=RIGHT),
            Column("Supplier", width="content"),
            Column("Location", width="content"),
        ]
        if auth.can(self.user, auth.PERM_VIEW_COST):
            columns.insert(6, Column("Avg Cost", width=110, align=RIGHT))
            columns.insert(7, Column("Stock Value", width=120, align=RIGHT))
        return columns

    def _build_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        title = common.screen_title("INVENTORY")
        row.addWidget(title)

        self.refresh_button = common.IconButton("refresh", 34)
        self.refresh_button.setToolTip("Reload from the database")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        row.addStretch()

        can_manage = auth.can(self.user, auth.PERM_MANAGE_ITEMS)
        can_delete = auth.can(self.user, auth.PERM_DELETE_RECORDS)
        can_order = auth.can(self.user, auth.PERM_CREATE_PURCHASE)

        self.new_button = common.action_button("New Item", self._new_item, "action")
        self.new_button.setEnabled(can_manage)
        if not can_manage:
            self.new_button.setToolTip("Your role cannot create items.")

        self.delete_button = common.action_button("Delete", self._delete_items, "danger")
        self.delete_button.setEnabled(False)
        if not can_delete:
            self.delete_button.setToolTip("Only an Owner can delete records.")

        self.order_button = common.action_button(
            "Order", lambda: self._reorder(to_maximum=False), "action",
            tooltip="Create purchase orders — you enter the quantity for each item.")
        self.order_button.setEnabled(False)

        self.order_max_button = common.action_button(
            "Order Max", lambda: self._reorder(to_maximum=True), "action",
            tooltip="Create purchase orders topping each item up to its maximum level.")
        self.order_max_button.setEnabled(False)

        if not can_order:
            for button in (self.order_button, self.order_max_button):
                button.setToolTip("Your role cannot raise purchase orders.")

        self.labels_button = common.action_button(
            "Labels", self._print_labels, "action",
            tooltip="Print barcode labels for the ticked items.")
        self.labels_button.setEnabled(False)

        self.export_button = common.action_button("Export", self._export, "action")

        for button in (self.new_button, self.delete_button, self.order_button,
                       self.order_max_button, self.labels_button, self.export_button):
            row.addWidget(button)
        return row

    def _print_labels(self):
        items = self.table.target_payloads()
        if not items:
            return
        try:
            path = documents.barcode_labels(items)
        except documents.DocumentError as exc:
            common.warn(self, "Nothing to print", str(exc))
            return
        except Exception as exc:
            common.error(self, "Could not create the labels", str(exc))
            return
        open_document(self, path, label="Labels ready")

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        row.addWidget(common.field_label("Warehouse"))
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(210)
        self.warehouse_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.warehouse_combo)

        row.addWidget(common.field_label("Category"))
        self.category_combo = common.ComboField()
        self.category_combo.setMinimumWidth(180)
        self.category_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.category_combo)

        self.low_stock_check = common.checkbox("Below minimum only")
        self.low_stock_check.toggled.connect(self.refresh)
        row.addWidget(self.low_stock_check)

        self.inactive_check = common.checkbox("Include inactive")
        self.inactive_check.toggled.connect(self.refresh)
        row.addWidget(self.inactive_check)

        row.addStretch()
        return row

    def _load_warehouses(self):
        warehouses = list(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name)  # noqa: E712
        )
        self.warehouse_combo.blockSignals(True)
        self.warehouse_combo.clear()
        self.warehouse_combo.addItem("All warehouses", ALL_WAREHOUSES)
        for warehouse in warehouses:
            self.warehouse_combo.addItem(f"{warehouse.name} ({warehouse.code})", warehouse)
        if self.user.warehouse is not None:
            self.warehouse_combo.select_record(self.user.warehouse)
        self.warehouse_combo.blockSignals(False)

        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All categories", None)
        for category in Category.select().order_by(Category.name):
            self.category_combo.addItem(category.name, category)
        self.category_combo.blockSignals(False)

    # --- Data -----------------------------------------------------------------------

    def current_warehouse(self):
        value = self.warehouse_combo.currentData()
        return None if value == ALL_WAREHOUSES else value

    def set_search_text(self, text):
        self._search_text = (text or "").strip().lower()
        self.refresh()

    def refresh(self):
        warehouse = self.current_warehouse()
        category = self.category_combo.currentData()

        # Join the lookups the table displays, so drawing a row costs no extra queries.
        query = (
            Item.select(Item, Supplier, Uom)
            .join(Supplier, JOIN.LEFT_OUTER, on=Item.preferred_supplier)
            .switch(Item)
            .join(Uom, JOIN.LEFT_OUTER, on=Item.base_uom)
        )
        if not self.inactive_check.isChecked():
            query = query.where(Item.is_active == True)  # noqa: E712
        if category is not None:
            query = query.where(Item.category == category)

        if self._search_text:
            term = f"%{self._search_text}%"
            query = query.where(
                (Item.name ** term) | (Item.sku ** term) | (Item.barcode ** term)
            )

        items = list(query.order_by(Item.name))

        # Every stock level in one query instead of one per item. Over a remote database
        # the per-item version cost hundreds of round trips and made the screen unusable.
        levels_by_item = defaultdict(list)
        if items:
            levels_query = (
                StockLevel.select(StockLevel, Warehouse)
                .join(Warehouse)
                .where(StockLevel.item.in_(items))
            )
            if warehouse is not None:
                levels_query = levels_query.where(StockLevel.warehouse == warehouse)
            for level in levels_query:
                levels_by_item[level.item_id].append(level)

        rows = []
        show_cost = auth.can(self.user, auth.PERM_VIEW_COST)
        total_value = Decimal("0")
        low_count = 0

        for item in items:
            levels = levels_by_item.get(item.id, [])
            on_hand = sum((Decimal(str(l.on_hand or 0)) for l in levels), Decimal("0"))
            reserved = sum((Decimal(str(l.reserved or 0)) for l in levels), Decimal("0"))
            available = on_hand - reserved

            minimum, maximum, location = self._thresholds(item, levels, warehouse)
            is_low = minimum > 0 and on_hand <= minimum

            if self.low_stock_check.isChecked() and not is_low:
                continue

            low_count += is_low
            value = on_hand * Decimal(str(item.avg_cost or 0))
            total_value += value

            cells = [
                text_cell(item.name, bold=True,
                          tooltip=item.description or item.name),
                text_cell(item.sku, tone=theme.TEXT_MUTED),
                qty_cell(on_hand, tone=theme.DANGER if is_low else None, bold=is_low),
                qty_cell(available,
                         tone=theme.WARNING if reserved > 0 else theme.TEXT_MUTED),
                qty_cell(minimum),
                qty_cell(maximum),
                text_cell(item.preferred_supplier.name if item.preferred_supplier else "—"),
                text_cell(location),
            ]
            if show_cost:
                cells.insert(6, money_cell(item.avg_cost))
                cells.insert(7, money_cell(value))

            rows.append(Row(
                cells=cells,
                payload=item,
                tint="#FFF6F5" if is_low else None,
                meta={"levels": levels, "on_hand": on_hand, "low": is_low},
            ))

        self.table.set_rows(rows)
        self._row_meta = {id(r.payload): r.meta for r in rows}

        scope = "all warehouses" if warehouse is None else warehouse.name
        parts = [f"{len(rows)} item(s) · {scope}"]
        if low_count:
            parts.append(f"{low_count} below minimum")
        if show_cost:
            parts.append(f"stock value {theme.money(total_value)}")
        self.status_label.setText("  ·  ".join(parts))
        self._update_button_states()

    @staticmethod
    def _level_min(level, item):
        """Per-warehouse override if set, otherwise the item default.

        Deliberately avoids StockLevel.effective_min, which reads level.item and would
        trigger a lazy fetch per row.
        """
        value = level.min_level if level.min_level is not None else item.default_min_level
        return Decimal(str(value or 0))

    @staticmethod
    def _level_max(level, item):
        value = level.max_level if level.max_level is not None else item.default_max_level
        return Decimal(str(value or 0))

    def _thresholds(self, item, levels, warehouse):
        """Min/max and a location label, which differ when viewing all warehouses."""
        if warehouse is not None:
            level = levels[0] if levels else None
            minimum = (self._level_min(level, item) if level
                       else Decimal(str(item.default_min_level or 0)))
            maximum = (self._level_max(level, item) if level
                       else Decimal(str(item.default_max_level or 0)))
            return minimum, maximum, warehouse.name

        minimum = sum(
            (self._level_min(l, item) for l in levels), Decimal("0"),
        ) or Decimal(str(item.default_min_level or 0))
        maximum = sum(
            (self._level_max(l, item) for l in levels), Decimal("0"),
        ) or Decimal(str(item.default_max_level or 0))

        stocked = [l for l in levels if Decimal(str(l.on_hand or 0)) > 0]
        if not stocked:
            location = "—"
        elif len(stocked) == 1:
            location = stocked[0].warehouse.name
        else:
            location = f"{len(stocked)} warehouses"
        return minimum, maximum, location

    # --- Actions --------------------------------------------------------------------

    def _update_button_states(self):
        targets = self.table.target_payloads()
        has_target = bool(targets)
        self.delete_button.setEnabled(
            has_target and auth.can(self.user, auth.PERM_DELETE_RECORDS)
        )
        can_order = has_target and auth.can(self.user, auth.PERM_CREATE_PURCHASE)
        self.order_button.setEnabled(can_order)
        self.order_max_button.setEnabled(can_order)
        self.labels_button.setEnabled(has_target)

    def _new_item(self):
        if not auth.can(self.user, auth.PERM_MANAGE_ITEMS):
            return
        dialog = ItemDialog(self, item=None, user=self.user)
        if dialog.exec():
            self.refresh()
            self._load_warehouses()

    def _edit_item(self, item):
        if not auth.can(self.user, auth.PERM_MANAGE_ITEMS):
            common.info(self, "View only",
                        f"{item.name}", "Your role cannot edit items.")
            return
        dialog = ItemDialog(self, item=item, user=self.user)
        if dialog.exec():
            self.refresh()

    def _delete_items(self):
        items = self.table.target_payloads()
        if not items:
            return
        if not auth.can(self.user, auth.PERM_DELETE_RECORDS):
            common.warn(self, "Not permitted", "Only an Owner can delete records.")
            return

        blocked, deletable = [], []
        for item in items:
            if inventory.on_hand(item) > 0:
                blocked.append(item)
            else:
                deletable.append(item)

        if blocked:
            names = ", ".join(i.name for i in blocked[:6])
            common.warn(
                self, "Some items still hold stock",
                f"{len(blocked)} item(s) cannot be deleted because stock is on hand:",
                f"{names}\n\nWrite the stock off with a stock adjustment first, or mark "
                f"the item inactive instead of deleting it.",
            )
        if not deletable:
            return

        names = "\n".join(f"  • {i.sku} — {i.name}" for i in deletable[:10])
        extra = f"\n  …and {len(deletable) - 10} more" if len(deletable) > 10 else ""
        if not common.confirm(
            self, "Delete items?",
            f"Permanently delete {len(deletable)} item(s)?",
            f"{names}{extra}\n\nThis also removes their movement history and cannot be "
            f"undone. Marking an item inactive is usually the better choice.",
            confirm_label="Delete permanently", destructive=True,
        ):
            return

        failed = []
        with db.atomic():
            for item in deletable:
                try:
                    label = f"{item.sku} — {item.name}"
                    item.delete_instance(recursive=True)
                    auth.record_audit(self.user, "DELETE", "Item", None,
                                      f"Deleted item {label}")
                except Exception as exc:
                    failed.append(f"{item.name}: {exc}")

        if failed:
            common.error(self, "Some items could not be deleted", "\n".join(failed[:5]))
        self.table.clear_checks()
        self.refresh()

    def _reorder(self, to_maximum=True):
        items = self.table.target_payloads()
        if not items:
            return
        if not auth.can(self.user, auth.PERM_CREATE_PURCHASE):
            common.warn(self, "Not permitted",
                        "Your role cannot raise purchase orders.")
            return

        warehouse = self.current_warehouse()
        if warehouse is None:
            common.warn(
                self, "Choose a warehouse",
                "Purchase orders are delivered to a specific warehouse.",
                "Pick one in the Warehouse selector above, then press Order again.",
            )
            return

        levels = []
        for item in items:
            levels.append(inventory.get_stock_level(item, warehouse))

        suggestions = purchasing.build_reorder_suggestions(levels, to_maximum=to_maximum)

        if to_maximum and not suggestions:
            common.info(
                self, "Nothing to order",
                "Every selected item is already at or above its maximum level.",
                "Use Order instead if you want to buy more anyway.",
            )
            return

        if not suggestions:
            # 'Order' mode: show every selected item so the buyer can type quantities.
            suggestions = [{
                "item": level.item,
                "warehouse": level.warehouse,
                "suggested": Decimal("0"),
                "on_hand": Decimal(str(level.on_hand or 0)),
                "minimum": Decimal(str(level.effective_min)),
                "maximum": Decimal(str(level.effective_max)),
                "supplier": level.item.preferred_supplier,
                "unit_cost": Decimal(str(level.item.last_purchase_cost
                                         or level.item.avg_cost or 0)),
            } for level in levels]

        dialog = ReorderDialog(self, suggestions=suggestions, user=self.user,
                               to_maximum=to_maximum)
        if dialog.exec():
            self.table.clear_checks()
            self.refresh()

    def _export(self):
        if not self.table.row_count():
            common.info(self, "Nothing to export", "There are no rows in the table.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export inventory", "inventory",
            "Excel workbook (*.xlsx);;CSV file (*.csv)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                if not path.lower().endswith(".csv"):
                    path += ".csv"
                self.table.export_csv(path, title="Inventory")
            else:
                if not path.lower().endswith(".xlsx"):
                    path += ".xlsx"
                self.table.export_xlsx(path, title="Inventory")
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))

    def _context_menu(self, position):
        item = self.table.current_payload()
        if item is None:
            return
        menu = QMenu(self)
        edit = menu.addAction("Edit item…")
        menu.addSeparator()
        order = menu.addAction("Order this item…")
        menu.addSeparator()
        copy = menu.addAction("Copy row")

        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is edit:
            self._edit_item(item)
        elif chosen is order:
            self.table.clear_checks()
            self._reorder(to_maximum=False)
        elif chosen is copy:
            self.table.copy_selection()
