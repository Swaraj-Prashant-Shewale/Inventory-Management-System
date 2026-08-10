"""Reorder dialog behind the Inventory screen's Order and Order Max buttons.

Order Max arrives pre-filled with the quantity needed to reach each item's maximum
level; Order arrives editable so the buyer types what they actually want. Either way the
result is draft purchase orders, grouped by supplier and warehouse.
"""
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from database.models import Supplier
from services import purchasing
from ui import theme
from ui.widgets import common

COL_ITEM, COL_WAREHOUSE, COL_ON_HAND, COL_MIN, COL_MAX, COL_QTY, COL_SUPPLIER, COL_COST = range(8)


class ReorderDialog(QDialog):
    def __init__(self, parent=None, suggestions=None, user=None, to_maximum=True):
        super().__init__(parent)
        self.user = user
        self.to_maximum = to_maximum
        self.suggestions = list(suggestions or [])
        self.created_orders = []

        self.setWindowTitle("Order to Maximum" if to_maximum else "Order Items")
        self.setMinimumSize(1120, 580)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Order to Maximum" if to_maximum else "Order Items")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        layout.addWidget(common.subtle(
            "Quantities are pre-filled to top each item up to its maximum level, rounded "
            "to whole purchase packs. Adjust anything you like — this creates draft "
            "purchase orders, grouped by supplier, that you confirm on the Receiving screen."
            if to_maximum else
            "Enter the quantity to order for each item. This creates draft purchase "
            "orders, grouped by supplier, that you confirm on the Receiving screen."
        ))

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Item", "Warehouse", "On Hand", "Min", "Max", "Order Qty", "Supplier",
            "Est. Cost",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(46)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_ITEM, QHeaderView.Stretch)
        # Item names are the one thing that must stay readable, so every other column is
        # pinned narrow rather than left to fight the stretch column for space.
        for col, width in (
            (COL_WAREHOUSE, 150), (COL_ON_HAND, 90), (COL_MIN, 80), (COL_MAX, 80),
            (COL_QTY, 130), (COL_SUPPLIER, 190), (COL_COST, 120),
        ):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Subtle")
        layout.addWidget(self.summary)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addWidget(common.action_button("Clear all quantities", self._clear_all,
                                               "ghost"))
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        self.create_button = common.action_button("Create Purchase Orders", self.submit,
                                                  "primary")
        buttons.addWidget(self.create_button)
        layout.addLayout(buttons)

        self._suppliers = list(
            Supplier.select().where(Supplier.is_active == True).order_by(Supplier.name)  # noqa: E712
        )
        self._rows = []
        self._populate()
        self._update_summary()

    # --- Table ----------------------------------------------------------------------

    def _populate(self):
        self.table.setRowCount(len(self.suggestions))
        for row, suggestion in enumerate(self.suggestions):
            item = suggestion["item"]

            name_cell = QTableWidgetItem(f"{item.name}  ·  {item.sku}")
            name_cell.setToolTip(f"{item.name}\nSKU {item.sku}")
            self.table.setItem(row, COL_ITEM, name_cell)

            self.table.setItem(row, COL_WAREHOUSE,
                               self._static(suggestion["warehouse"].name))
            self.table.setItem(row, COL_ON_HAND,
                               self._static(theme.quantity(suggestion["on_hand"]), Qt.AlignRight))
            self.table.setItem(row, COL_MIN,
                               self._static(theme.quantity(suggestion["minimum"]), Qt.AlignRight))
            self.table.setItem(row, COL_MAX,
                               self._static(theme.quantity(suggestion["maximum"]), Qt.AlignRight))

            allow_decimal = bool(item.base_uom and item.base_uom.allow_decimal)
            qty_spin = common.DecimalSpin(decimals=3 if allow_decimal else 0)
            qty_spin.setValue(float(suggestion["suggested"]))
            qty_spin.valueChanged.connect(
                lambda _value, r=row: self._on_quantity_changed(r)
            )
            self.table.setCellWidget(row, COL_QTY, qty_spin)

            supplier_combo = common.ComboField(allow_blank=True,
                                               blank_text="— choose supplier —")
            supplier_combo.load(self._suppliers, label=lambda s: s.name,
                                selected=suggestion.get("supplier"))
            self.table.setCellWidget(row, COL_SUPPLIER, supplier_combo)

            cost_cell = self._static("", Qt.AlignRight)
            self.table.setItem(row, COL_COST, cost_cell)

            self._rows.append({
                "suggestion": suggestion,
                "qty": qty_spin,
                "supplier": supplier_combo,
                "cost_cell": cost_cell,
            })
            self._on_quantity_changed(row)

    def _static(self, text, align=Qt.AlignLeft | Qt.AlignVCenter):
        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemIsEnabled)
        cell.setTextAlignment(align | Qt.AlignVCenter if align == Qt.AlignRight else align)
        return cell

    def _on_quantity_changed(self, row):
        entry = self._rows[row]
        quantity = Decimal(str(entry["qty"].value()))
        unit_cost = entry["suggestion"]["unit_cost"] or Decimal("0")
        entry["cost_cell"].setText(theme.money(quantity * unit_cost))
        self._update_summary()

    def _clear_all(self):
        for entry in self._rows:
            entry["qty"].setValue(0)

    def _update_summary(self):
        lines = 0
        total = Decimal("0")
        suppliers = set()
        for entry in self._rows:
            quantity = Decimal(str(entry["qty"].value()))
            if quantity <= 0:
                continue
            lines += 1
            total += quantity * (entry["suggestion"]["unit_cost"] or Decimal("0"))
            supplier = entry["supplier"].current()
            if supplier is not None:
                suppliers.add((supplier.id, entry["suggestion"]["warehouse"].id))
        order_count = len(suppliers)
        self.summary.setText(
            f"{lines} line(s) · estimated {theme.money(total)} excluding GST · "
            f"will create {order_count} purchase order(s)"
        )
        self.create_button.setEnabled(lines > 0)

    # --- Save -----------------------------------------------------------------------

    def submit(self):
        self.error_label.setVisible(False)

        requests = []
        missing_supplier = []
        for entry in self._rows:
            quantity = Decimal(str(entry["qty"].value()))
            if quantity <= 0:
                continue
            supplier = entry["supplier"].current()
            if supplier is None:
                missing_supplier.append(entry["suggestion"]["item"].name)
                continue
            requests.append({
                "item": entry["suggestion"]["item"],
                "warehouse": entry["suggestion"]["warehouse"],
                "quantity": quantity,
                "unit_cost": entry["suggestion"]["unit_cost"],
                "supplier": supplier,
            })

        if missing_supplier:
            self.error_label.setText(
                "Choose a supplier for: " + ", ".join(missing_supplier[:5])
                + ("…" if len(missing_supplier) > 5 else "")
            )
            self.error_label.setVisible(True)
            return

        if not requests:
            self.error_label.setText("Enter a quantity for at least one item.")
            self.error_label.setVisible(True)
            return

        try:
            orders, unassigned = purchasing.create_orders_from_requests(
                requests, user=self.user
            )
        except Exception as exc:
            self.error_label.setText(f"Could not create purchase orders: {exc}")
            self.error_label.setVisible(True)
            return

        self.created_orders = orders
        numbers = "\n".join(f"  • {o.number} — {o.supplier.name} "
                            f"({len(list(o.lines))} lines, {theme.money(o.total)})"
                            for o in orders)
        common.info(
            self, "Purchase orders created",
            f"{len(orders)} draft purchase order(s) created:",
            numbers + "\n\nThey are drafts — open Receiving to confirm and send them.",
        )
        self.accept()
