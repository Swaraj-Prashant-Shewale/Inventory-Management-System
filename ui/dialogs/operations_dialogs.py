"""Dialogs for stock adjustments, warehouse transfers and cycle counts."""
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.models import AdjustmentReason, Item, Warehouse
from services import inventory, warehouse_ops
from ui import theme
from ui.widgets import common
from ui.widgets.line_editor import ItemPicker


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


class SimpleLineGrid(QWidget):
    """Item + quantity grid, used where tax and pricing are irrelevant."""

    def __init__(self, parent=None, quantity_label="Quantity", allow_negative=False,
                 show_on_hand=True, note_column=True):
        super().__init__(parent)
        self.allow_negative = allow_negative
        self.show_on_hand = show_on_hand
        self.note_column = note_column
        self.warehouse = None
        self._rows = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        headers = ["Item"]
        if show_on_hand:
            headers.append("On Hand")
        headers.append(quantity_label)
        if note_column:
            headers.append("Note")
        headers.append("")
        self._headers = headers

        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(46)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for index, title in enumerate(headers):
            if index == 0:
                continue
            head.setSectionResizeMode(index, QHeaderView.Fixed)
            self.table.setColumnWidth(index, {"On Hand": 110, "": 44}.get(title, 140))
            if title == "Note":
                head.setSectionResizeMode(index, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        controls.addWidget(common.action_button("+ Add Line", self.add_row, "ghost"))
        controls.addStretch()
        self.summary = QLabel("")
        self.summary.setObjectName("Subtle")
        controls.addWidget(self.summary)
        layout.addLayout(controls)

        self._items = list(
            Item.select().where(Item.is_active == True).order_by(Item.name)  # noqa: E712
        )

    def set_warehouse(self, warehouse):
        self.warehouse = warehouse
        for entry in self._rows:
            self._refresh_on_hand(entry)

    def add_row(self, item=None, quantity=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        column = 0

        picker = ItemPicker(self._items)
        self.table.setCellWidget(row, column, picker)
        column += 1

        on_hand_cell = None
        if self.show_on_hand:
            on_hand_cell = QTableWidgetItem("—")
            on_hand_cell.setFlags(Qt.ItemIsEnabled)
            on_hand_cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, column, on_hand_cell)
            column += 1

        qty = common.DecimalSpin(decimals=3)
        if self.allow_negative:
            qty.setMinimum(-99_999_999.0)
        self.table.setCellWidget(row, column, qty)
        column += 1

        note = None
        if self.note_column:
            note = QLineEdit()
            note.setPlaceholderText("Optional")
            self.table.setCellWidget(row, column, note)
            column += 1

        remove = QPushButton("✕")
        remove.setCursor(Qt.PointingHandCursor)
        remove.setStyleSheet(f"color: {theme.DANGER}; font-weight: 700; border: none;")
        self.table.setCellWidget(row, column, remove)

        entry = {"picker": picker, "qty": qty, "note": note, "on_hand": on_hand_cell}
        self._rows.append(entry)

        picker.currentIndexChanged.connect(lambda _i, e=entry: self._on_item(e))
        picker.lineEdit().editingFinished.connect(
            lambda e=entry: (e["picker"].resolve_typed_text(), self._on_item(e)))
        qty.valueChanged.connect(self._update_summary)
        remove.clicked.connect(lambda _c=False, e=entry: self.remove_row(e))

        if item is not None:
            picker.select_item(item)
        if quantity is not None:
            qty.setValue(float(quantity))
        self._update_summary()
        return entry

    def _on_item(self, entry):
        item = entry["picker"].current_item()
        if item is not None:
            allow_decimal = bool(item.base_uom and item.base_uom.allow_decimal)
            entry["qty"].setDecimals(3 if allow_decimal else 0)
        self._refresh_on_hand(entry)
        self._update_summary()

    def _refresh_on_hand(self, entry):
        if entry["on_hand"] is None:
            return
        item = entry["picker"].current_item()
        if item is None or self.warehouse is None:
            entry["on_hand"].setText("—")
            return
        entry["on_hand"].setText(
            theme.quantity(inventory.on_hand(item, self.warehouse))
        )

    def remove_row(self, entry):
        if entry not in self._rows:
            return
        index = self._rows.index(entry)
        self._rows.pop(index)
        self.table.removeRow(index)
        self._update_summary()

    def _update_summary(self):
        count = sum(1 for e in self._rows
                    if e["picker"].current_item() is not None
                    and _dec(e["qty"].value()) != 0)
        self.summary.setText(f"{count} line(s)")

    def lines(self):
        result = []
        for entry in self._rows:
            item = entry["picker"].current_item() or entry["picker"].resolve_typed_text()
            quantity = _dec(entry["qty"].value())
            if item is None or quantity == 0:
                continue
            result.append({"item": item, "quantity": quantity,
                           "note": entry["note"].text().strip() if entry["note"] else None})
        return result

    def problem(self):
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
            if _dec(entry["qty"].value()) == 0:
                return f"Line {index} ({item.name}): enter a quantity."
            if item.id in seen:
                return f"{item.name} appears on more than one line."
            seen.add(item.id)
        return None


class AdjustmentDialog(QDialog):
    """Propose a stock write-up or write-down. Nothing moves until it is approved."""

    def __init__(self, parent=None, user=None, warehouse=None):
        super().__init__(parent)
        self.user = user
        self.adjustment = None

        self.setWindowTitle("New Stock Adjustment")
        self.setMinimumSize(940, 560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("New Stock Adjustment")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            "Use a positive quantity for stock found and a negative one for stock lost "
            "or damaged. The adjustment is recorded now but only moves stock once a "
            "manager approves it."
        ))

        form = QHBoxLayout()
        left = QFormLayout()
        left.setVerticalSpacing(10)
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(240)
        self.warehouse_combo.load(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name),  # noqa: E712
            label=lambda w: f"{w.name} ({w.code})")
        if warehouse is not None:
            self.warehouse_combo.select_record(warehouse)
        elif user is not None and user.warehouse is not None:
            self.warehouse_combo.select_record(user.warehouse)
        self.warehouse_combo.currentIndexChanged.connect(self._on_warehouse)

        self.reason_combo = common.ComboField()
        self.reason_combo.setMinimumWidth(240)
        self.reason_combo.load_choices(
            [(r, AdjustmentReason.LABELS[r]) for r in AdjustmentReason.ALL])
        left.addRow(common.field_label("Warehouse *"), self.warehouse_combo)
        left.addRow(common.field_label("Reason *"), self.reason_combo)

        right = QFormLayout()
        right.setVerticalSpacing(10)
        self.notes_input = QPlainTextEdit()
        self.notes_input.setFixedHeight(62)
        right.addRow(common.field_label("Notes"), self.notes_input)

        form.addLayout(left, 1)
        form.addLayout(right, 1)
        layout.addLayout(form)

        self.grid = SimpleLineGrid(quantity_label="Adjust By (±)", allow_negative=True)
        self.grid.set_warehouse(self.warehouse_combo.current())
        self.grid.add_row()
        layout.addWidget(self.grid, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        self.save_button = common.action_button("Save for Approval", self.submit, "action")
        buttons.addWidget(self.save_button)
        if auth_can_approve(user):
            buttons.addWidget(common.action_button(
                "Save and Approve", lambda: self.submit(approve=True), "primary"))
        layout.addLayout(buttons)

    def _on_warehouse(self):
        self.grid.set_warehouse(self.warehouse_combo.current())

    def submit(self, approve=False):
        self.error_label.setVisible(False)
        warehouse = self.warehouse_combo.current()
        if warehouse is None:
            self.error_label.setText("Choose a warehouse.")
            self.error_label.setVisible(True)
            return

        problem = self.grid.problem()
        if problem:
            self.error_label.setText(problem)
            self.error_label.setVisible(True)
            return

        lines = [{"item": line["item"], "quantity_delta": line["quantity"],
                  "note": line["note"]} for line in self.grid.lines()]
        try:
            self.adjustment = warehouse_ops.create_adjustment(
                warehouse, lines, reason=self.reason_combo.current(), user=self.user,
                notes=self.notes_input.toPlainText().strip() or None,
            )
            if approve:
                warehouse_ops.approve_adjustment(self.adjustment, self.user)
            self.accept()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)


class TransferDialog(QDialog):
    """Move stock between warehouses."""

    def __init__(self, parent=None, user=None, warehouse=None):
        super().__init__(parent)
        self.user = user
        self.transfer = None

        self.setWindowTitle("New Stock Transfer")
        self.setMinimumSize(940, 560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("New Stock Transfer")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            "Stock leaves the source warehouse when you dispatch, and arrives at the "
            "destination when it is received. In between it is in transit and belongs "
            "to neither."
        ))

        form = QHBoxLayout()
        left = QFormLayout()
        left.setVerticalSpacing(10)
        warehouses = list(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name))  # noqa: E712
        self.from_combo = common.ComboField()
        self.from_combo.setMinimumWidth(230)
        self.from_combo.load(warehouses, label=lambda w: f"{w.name} ({w.code})")
        self.to_combo = common.ComboField(allow_blank=True,
                                          blank_text="— choose destination —")
        self.to_combo.setMinimumWidth(230)
        self.to_combo.load(warehouses, label=lambda w: f"{w.name} ({w.code})")
        if warehouse is not None:
            self.from_combo.select_record(warehouse)
        elif user is not None and user.warehouse is not None:
            self.from_combo.select_record(user.warehouse)
        self.from_combo.currentIndexChanged.connect(self._on_source)
        left.addRow(common.field_label("From *"), self.from_combo)
        left.addRow(common.field_label("To *"), self.to_combo)

        right = QFormLayout()
        right.setVerticalSpacing(10)
        self.truck_input = QLineEdit()
        self.truck_input.setPlaceholderText("e.g. MH12AB1234")
        self.notes_input = QLineEdit()
        right.addRow(common.field_label("Truck HSRP"), self.truck_input)
        right.addRow(common.field_label("Notes"), self.notes_input)

        form.addLayout(left, 1)
        form.addLayout(right, 1)
        layout.addLayout(form)

        self.grid = SimpleLineGrid(quantity_label="Quantity", note_column=False)
        self.grid.set_warehouse(self.from_combo.current())
        self.grid.add_row()
        layout.addWidget(self.grid, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Save Draft", self.submit, "action"))
        buttons.addWidget(common.action_button(
            "Save and Dispatch", lambda: self.submit(dispatch=True), "primary"))
        layout.addLayout(buttons)

    def _on_source(self):
        self.grid.set_warehouse(self.from_combo.current())

    def submit(self, dispatch=False):
        self.error_label.setVisible(False)
        source = self.from_combo.current()
        destination = self.to_combo.current()
        if source is None or destination is None:
            self.error_label.setText("Choose both a source and a destination warehouse.")
            self.error_label.setVisible(True)
            return

        problem = self.grid.problem()
        if problem:
            self.error_label.setText(problem)
            self.error_label.setVisible(True)
            return

        try:
            self.transfer = warehouse_ops.create_transfer(
                source, destination, self.grid.lines(), user=self.user,
                truck_hsrp=self.truck_input.text().strip() or None,
                notes=self.notes_input.text().strip() or None,
            )
            if dispatch:
                warehouse_ops.dispatch_transfer(self.transfer, self.user)
            self.accept()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)


class CycleCountDialog(QDialog):
    """Enter counted quantities against an open count."""

    def __init__(self, parent=None, count=None, user=None):
        super().__init__(parent)
        self.count = count
        self.user = user
        self.approved = False

        self.setWindowTitle(f"Cycle Count {count.number}")
        self.setMinimumSize(900, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel(f"Cycle Count — {count.number}")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            f"{count.warehouse.name}. Enter what you physically counted for every line, "
            f"including zeros. Approving posts an adjustment for each difference so the "
            f"system matches the shelf."
        ))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Item", "System Says", "Counted",
                                              "Variance"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(46)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 140), (2, 150), (3, 140)):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
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
        buttons.addWidget(common.action_button(
            "Accept system quantities", self._accept_system, "ghost"))
        buttons.addStretch()
        buttons.addWidget(common.action_button("Close", self.reject, "action"))
        buttons.addWidget(common.action_button("Save Counts", self.save, "action"))
        if auth_can_approve(user):
            buttons.addWidget(common.action_button(
                "Save and Approve", lambda: self.save(approve=True), "primary"))
        layout.addLayout(buttons)

        self._rows = []
        self._populate()

    def _populate(self):
        lines = list(self.count.lines)
        self.table.setRowCount(len(lines))
        right = Qt.AlignRight | Qt.AlignVCenter

        for row, line in enumerate(lines):
            name = QTableWidgetItem(f"{line.item.name}  ·  {line.item.sku}")
            name.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 0, name)

            expected = QTableWidgetItem(theme.quantity(line.expected_quantity))
            expected.setFlags(Qt.ItemIsEnabled)
            expected.setTextAlignment(right)
            self.table.setItem(row, 1, expected)

            allow_decimal = bool(line.item.base_uom and line.item.base_uom.allow_decimal)
            counted = common.DecimalSpin(decimals=3 if allow_decimal else 0)
            counted.setMinimum(0.0)
            if line.counted_quantity is not None:
                counted.setValue(float(line.counted_quantity))
            self.table.setCellWidget(row, 2, counted)

            variance = QTableWidgetItem("—")
            variance.setFlags(Qt.ItemIsEnabled)
            variance.setTextAlignment(right)
            self.table.setItem(row, 3, variance)

            entry = {"line": line, "counted": counted, "variance": variance,
                     "touched": line.counted_quantity is not None}
            self._rows.append(entry)
            counted.valueChanged.connect(lambda _v, e=entry: self._on_count(e))
            self._on_count(entry, initial=True)

    def _on_count(self, entry, initial=False):
        from PySide6.QtGui import QBrush, QColor
        if not initial:
            entry["touched"] = True
        expected = _dec(entry["line"].expected_quantity)
        counted = _dec(entry["counted"].value())
        if not entry["touched"]:
            entry["variance"].setText("—")
            entry["variance"].setForeground(QBrush(QColor(theme.TEXT_MUTED)))
        else:
            variance = counted - expected
            entry["variance"].setText(
                "0" if variance == 0 else f"{'+' if variance > 0 else ''}"
                                          f"{theme.quantity(variance)}")
            entry["variance"].setForeground(QBrush(QColor(
                theme.TEXT_MUTED if variance == 0
                else (theme.SUCCESS if variance > 0 else theme.DANGER))))
        self._update_summary()

    def _accept_system(self):
        for entry in self._rows:
            entry["counted"].setValue(float(entry["line"].expected_quantity))
            entry["touched"] = True
            self._on_count(entry, initial=True)

    def _update_summary(self):
        counted = sum(1 for e in self._rows if e["touched"])
        variances = sum(1 for e in self._rows if e["touched"]
                        and _dec(e["counted"].value()) != _dec(e["line"].expected_quantity))
        self.summary.setText(
            f"{counted} of {len(self._rows)} counted  ·  {variances} variance(s)"
        )

    def save(self, approve=False):
        self.error_label.setVisible(False)
        values = {e["line"].id: _dec(e["counted"].value())
                  for e in self._rows if e["touched"]}
        try:
            warehouse_ops.record_counts(self.count, values, user=self.user)
            if approve:
                adjustment = warehouse_ops.approve_cycle_count(self.count, self.user)
                self.approved = True
                if adjustment is None:
                    common.info(self, "Count approved",
                                f"{self.count.number} matched the system exactly.",
                                "No adjustment was needed.")
                else:
                    common.info(
                        self, "Count approved",
                        f"{self.count.number} approved.",
                        f"Variances were posted as adjustment {adjustment.number} and "
                        f"stock now matches your count.",
                    )
            self.accept()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)


def auth_can_approve(user):
    from services import auth
    return auth.can(user, auth.PERM_APPROVE_ADJUSTMENT)
