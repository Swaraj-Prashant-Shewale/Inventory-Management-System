"""Editable order-line grid, shared by the purchase and sales order dialogs.

One widget, two modes: purchasing edits unit cost, sales edits unit price and discount
and resolves the customer's price list. Totals recalculate live, including the GST split.
"""
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.models import ZERO, Item
from services import gst, pricing
from ui import theme
from ui.widgets import common

PURCHASE = "purchase"
SALES = "sales"


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


class ItemPicker(QComboBox):
    """Editable combo that filters on SKU, name or barcode — scanner friendly."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItem("", None)
        for item in items:
            label = f"{item.sku} — {item.name}"
            self.addItem(label, item)

        completer = QCompleter(self)
        completer.setModel(self.model())
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.setCompleter(completer)
        self.lineEdit().setPlaceholderText("Type or scan an item…")

    def current_item(self):
        return self.currentData()

    def select_item(self, item):
        for i in range(self.count()):
            data = self.itemData(i)
            if data is not None and data.id == item.id:
                self.setCurrentIndex(i)
                return True
        return False

    def resolve_typed_text(self):
        """Accept a scanned barcode or an exact SKU typed into the field."""
        text = self.currentText().strip()
        if not text or self.currentData() is not None:
            return self.currentData()
        match = Item.get_or_none((Item.barcode == text) | (Item.sku == text.upper()))
        if match is not None:
            self.select_item(match)
        return match


class LineEditor(QWidget):
    """Grid of order lines with live totals."""

    changed = Signal()

    def __init__(self, mode=PURCHASE, parent=None, allow_edit=True):
        super().__init__(parent)
        self.mode = mode
        self.allow_edit = allow_edit
        self.interstate = False
        self.customer = None
        self._rows = []
        self._items = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._rate_title = "Unit Cost" if mode == PURCHASE else "Unit Price"
        headers = ["Item", "Qty", self._rate_title]
        if mode == SALES:
            headers.append("Disc %")
        headers += ["GST %", "Line Total", ""]
        self._columns = headers

        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(46)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        widths = {"Qty": 100, "Unit Cost": 130, "Unit Price": 130, "Disc %": 90,
                  "GST %": 90, "Line Total": 130, "": 44}
        for index, title in enumerate(headers):
            if index == 0:
                continue
            header.setSectionResizeMode(index, QHeaderView.Fixed)
            self.table.setColumnWidth(index, widths.get(title, 100))
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.add_button = common.action_button("+ Add Line", self.add_row, "ghost")
        self.add_button.setEnabled(allow_edit)
        controls.addWidget(self.add_button)
        controls.addStretch()

        self.totals_label = QLabel("")
        self.totals_label.setStyleSheet(
            f"font-size: 14px; color: {theme.TEXT}; font-weight: 600;"
        )
        self.totals_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        controls.addWidget(self.totals_label)
        layout.addLayout(controls)

        self.reload_items()

    # --- Setup ----------------------------------------------------------------------

    def reload_items(self):
        self._items = list(
            Item.select().where(Item.is_active == True).order_by(Item.name)  # noqa: E712
        )

    def set_tax_context(self, warehouse, party):
        """Recompute the GST split when the warehouse or trading partner changes."""
        self.interstate = gst.is_interstate(
            getattr(warehouse, "state_code", None), getattr(party, "state_code", None)
        )
        self.recalculate()

    def set_customer(self, customer):
        """Sales mode: repricing every line against the new customer's price list."""
        self.customer = customer
        if self.mode != SALES:
            return
        for row in self._rows:
            item = row["picker"].current_item()
            if item is not None:
                row["rate"].setValue(float(pricing.resolve_price(
                    item, customer, _dec(row["qty"].value())
                )))
        self.recalculate()

    # --- Rows -----------------------------------------------------------------------

    def add_row(self, item=None, quantity=None, rate=None, gst_rate=None,
                discount=None):
        row_index = self.table.rowCount()
        self.table.insertRow(row_index)

        picker = ItemPicker(self._items)
        picker.setEnabled(self.allow_edit)
        self.table.setCellWidget(row_index, 0, picker)

        qty = common.DecimalSpin(decimals=3)
        qty.setEnabled(self.allow_edit)
        self.table.setCellWidget(row_index, 1, qty)

        rate_spin = common.DecimalSpin(decimals=2, prefix="₹ ")
        rate_spin.setEnabled(self.allow_edit)
        self.table.setCellWidget(row_index, 2, rate_spin)

        column = 3
        discount_spin = None
        if self.mode == SALES:
            discount_spin = common.DecimalSpin(decimals=2, maximum=100.0)
            discount_spin.setEnabled(self.allow_edit)
            self.table.setCellWidget(row_index, column, discount_spin)
            column += 1

        gst_spin = common.DecimalSpin(decimals=2, maximum=100.0)
        gst_spin.setEnabled(self.allow_edit)
        self.table.setCellWidget(row_index, column, gst_spin)
        column += 1

        total_cell = QTableWidgetItem("₹0.00")
        total_cell.setFlags(Qt.ItemIsEnabled)
        total_cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row_index, column, total_cell)
        column += 1

        remove = QPushButton("✕")
        remove.setObjectName("Ghost")
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip("Remove this line")
        remove.setEnabled(self.allow_edit)
        remove.setStyleSheet(f"color: {theme.DANGER}; font-weight: 700; border: none;")
        self.table.setCellWidget(row_index, column, remove)

        entry = {"picker": picker, "qty": qty, "rate": rate_spin,
                 "discount": discount_spin, "gst": gst_spin, "total": total_cell,
                 "remove": remove}
        self._rows.append(entry)

        picker.currentIndexChanged.connect(lambda _i, e=entry: self._on_item_changed(e))
        picker.lineEdit().editingFinished.connect(lambda e=entry: self._on_text_entered(e))
        qty.valueChanged.connect(lambda _v, e=entry: self._on_quantity_changed(e))
        rate_spin.valueChanged.connect(lambda _v: self.recalculate())
        gst_spin.valueChanged.connect(lambda _v: self.recalculate())
        if discount_spin is not None:
            discount_spin.valueChanged.connect(lambda _v: self.recalculate())
        remove.clicked.connect(lambda _checked=False, e=entry: self.remove_row(e))

        if item is not None:
            picker.select_item(item)
        if quantity is not None:
            qty.setValue(float(quantity))
        if rate is not None:
            rate_spin.setValue(float(rate))
        if gst_rate is not None:
            gst_spin.setValue(float(gst_rate))
        if discount is not None and discount_spin is not None:
            discount_spin.setValue(float(discount))

        self.recalculate()
        return entry

    def remove_row(self, entry):
        if entry not in self._rows:
            return
        index = self._rows.index(entry)
        self._rows.pop(index)
        self.table.removeRow(index)
        self.recalculate()

    def clear(self):
        self._rows.clear()
        self.table.setRowCount(0)
        self.recalculate()

    def load_lines(self, lines):
        """Populate from existing order lines."""
        self.clear()
        for line in lines:
            self.add_row(
                item=line.item,
                quantity=line.quantity,
                rate=(line.unit_cost if self.mode == PURCHASE else line.unit_price),
                gst_rate=line.gst_rate,
                discount=getattr(line, "discount_percent", None),
            )

    # --- Reactions ------------------------------------------------------------------

    def _on_item_changed(self, entry):
        item = entry["picker"].current_item()
        if item is None:
            self.recalculate()
            return
        entry["gst"].setValue(float(item.gst_rate or 0))
        if self.mode == PURCHASE:
            entry["rate"].setValue(float(item.last_purchase_cost or item.avg_cost or 0))
        else:
            entry["rate"].setValue(float(pricing.resolve_price(
                item, self.customer, _dec(entry["qty"].value())
            )))
            entry["rate"].setToolTip(pricing.price_explanation(
                item, self.customer, _dec(entry["qty"].value())
            ))
        allow_decimal = bool(item.base_uom and item.base_uom.allow_decimal)
        entry["qty"].setDecimals(3 if allow_decimal else 0)
        if entry["qty"].value() == 0:
            entry["qty"].setValue(1)
        self.recalculate()

    def _on_text_entered(self, entry):
        """A scanned barcode lands here — resolve it to an item."""
        if entry["picker"].current_item() is None:
            entry["picker"].resolve_typed_text()

    def _on_quantity_changed(self, entry):
        """Sales: crossing a quantity break should reprice the line."""
        if self.mode == SALES and self.customer is not None:
            item = entry["picker"].current_item()
            if item is not None:
                new_price = pricing.resolve_price(item, self.customer,
                                                  _dec(entry["qty"].value()))
                if _dec(entry["rate"].value()) != new_price:
                    entry["rate"].setValue(float(new_price))
        self.recalculate()

    # --- Totals ---------------------------------------------------------------------

    def recalculate(self):
        subtotal = ZERO
        tax_total = ZERO

        for entry in self._rows:
            quantity = _dec(entry["qty"].value())
            rate = _dec(entry["rate"].value())
            gross = quantity * rate
            if entry["discount"] is not None:
                gross -= gross * _dec(entry["discount"].value()) / Decimal("100")
            line_total = gross.quantize(Decimal("0.01"))
            entry["total"].setText(theme.money(line_total))
            subtotal += line_total
            cgst, sgst, igst = gst.split_tax(line_total, _dec(entry["gst"].value()),
                                             self.interstate)
            tax_total += cgst + sgst + igst

        total = subtotal + tax_total
        tax_label = "IGST" if self.interstate else "CGST+SGST"
        self.totals_label.setText(
            f"Subtotal {theme.money(subtotal)}   ·   {tax_label} "
            f"{theme.money(tax_total)}   ·   Total {theme.money(total)}"
        )
        self.changed.emit()
        return subtotal, tax_total, total

    def totals(self):
        return self.recalculate()

    # --- Output ---------------------------------------------------------------------

    def lines(self):
        """Valid lines as dicts ready for the service layer."""
        result = []
        for entry in self._rows:
            item = entry["picker"].current_item() or entry["picker"].resolve_typed_text()
            quantity = _dec(entry["qty"].value())
            if item is None or quantity <= 0:
                continue
            line = {
                "item": item,
                "quantity": quantity,
                "gst_rate": _dec(entry["gst"].value()),
                "hsn_code": item.hsn_code,
            }
            if self.mode == PURCHASE:
                line["unit_cost"] = _dec(entry["rate"].value())
            else:
                line["unit_price"] = _dec(entry["rate"].value())
                line["discount_percent"] = _dec(entry["discount"].value())
            result.append(line)
        return result

    def problem(self):
        """Why the lines aren't acceptable yet, or None."""
        if not self._rows:
            return "Add at least one line."
        seen = set()
        for index, entry in enumerate(self._rows, start=1):
            item = entry["picker"].current_item() or entry["picker"].resolve_typed_text()
            typed = entry["picker"].currentText().strip()
            if item is None:
                if typed:
                    return f"Line {index}: '{typed}' does not match any item."
                return f"Line {index}: choose an item."
            if _dec(entry["qty"].value()) <= 0:
                return f"Line {index} ({item.name}): enter a quantity."
            if item.id in seen:
                return (f"{item.name} appears on more than one line — "
                        f"combine them into a single line.")
            seen.add(item.id)
        return None
