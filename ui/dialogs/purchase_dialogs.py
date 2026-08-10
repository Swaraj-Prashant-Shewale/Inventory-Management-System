"""Purchase order editor and the goods receipt dialog."""
from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.models import PurchaseStatus, Supplier, Warehouse
from services import purchasing
from ui import theme
from ui.widgets import common
from ui.widgets.line_editor import PURCHASE, LineEditor


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


class PurchaseOrderDialog(QDialog):
    """Create or edit a draft purchase order."""

    def __init__(self, parent=None, order=None, user=None, warehouse=None):
        super().__init__(parent)
        self.order = order
        self.user = user
        self.is_new = order is None
        self.saved_order = None
        self.read_only = order is not None and order.status != PurchaseStatus.DRAFT

        title = "New Purchase Order" if self.is_new else f"Purchase Order {order.number}"
        self.setWindowTitle(title)
        self.setMinimumSize(1060, 660)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading_row = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("ScreenTitle")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        if not self.is_new:
            pill = common.StatusPill(
                PurchaseStatus.LABELS.get(order.status, order.status),
                _po_tone(order.status),
            )
            heading_row.addWidget(pill)
        layout.addLayout(heading_row)

        if self.read_only:
            layout.addWidget(common.subtle(
                "This order has been sent to the supplier, so its lines are locked. "
                "Use Receive to book in what arrives."
            ))

        layout.addWidget(self._build_header_form())

        self.line_editor = LineEditor(mode=PURCHASE, allow_edit=not self.read_only)
        layout.addWidget(self.line_editor, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button(
            "Close" if self.read_only else "Cancel", self.reject, "action"))
        if not self.read_only:
            self.save_button = common.action_button("Save Draft", self.save, "action")
            buttons.addWidget(self.save_button)
            self.confirm_button = common.action_button(
                "Save and Send to Supplier", lambda: self.save(confirm=True), "primary")
            buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

        self._load()

    def _build_header_form(self):
        panel = QWidget()
        form = QHBoxLayout(panel)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(18)

        left = QFormLayout()
        left.setVerticalSpacing(10)
        self.supplier_combo = common.ComboField(allow_blank=True,
                                                blank_text="— choose supplier —")
        self.supplier_combo.setMinimumWidth(260)
        self.supplier_combo.currentIndexChanged.connect(self._on_party_changed)
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(260)
        self.warehouse_combo.currentIndexChanged.connect(self._on_party_changed)
        left.addRow(common.field_label("Supplier *"), self.supplier_combo)
        left.addRow(common.field_label("Deliver to *"), self.warehouse_combo)

        right = QFormLayout()
        right.setVerticalSpacing(10)
        self.eta_input = QDateEdit()
        self.eta_input.setCalendarPopup(True)
        self.eta_input.setDisplayFormat("dd MMM yyyy")
        self.eta_input.setDate(QDate.currentDate().addDays(7))
        self.invoice_input = QLineEdit()
        self.invoice_input.setPlaceholderText("Supplier's invoice number, if known")
        right.addRow(common.field_label("Expected (ETA)"), self.eta_input)
        right.addRow(common.field_label("Supplier invoice"), self.invoice_input)

        notes_form = QFormLayout()
        notes_form.setVerticalSpacing(10)
        self.notes_input = QPlainTextEdit()
        self.notes_input.setFixedHeight(64)
        notes_form.addRow(common.field_label("Notes"), self.notes_input)

        form.addLayout(left, 1)
        form.addLayout(right, 1)
        form.addLayout(notes_form, 1)
        return panel

    def _load(self):
        self.supplier_combo.load(
            Supplier.select().where(Supplier.is_active == True).order_by(Supplier.name),  # noqa: E712
            label=lambda s: f"{s.name} ({s.code})",
        )
        self.warehouse_combo.load(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name),  # noqa: E712
            label=lambda w: f"{w.name} ({w.code})",
        )

        if self.order is not None:
            self.supplier_combo.select_record(self.order.supplier)
            self.warehouse_combo.select_record(self.order.warehouse)
            if self.order.expected_date:
                self.eta_input.setDate(QDate(self.order.expected_date.year,
                                             self.order.expected_date.month,
                                             self.order.expected_date.day))
            self.invoice_input.setText(self.order.supplier_invoice_no or "")
            self.notes_input.setPlainText(self.order.notes or "")
            self.line_editor.load_lines(list(self.order.lines))
            for widget in (self.supplier_combo, self.warehouse_combo, self.eta_input,
                           self.invoice_input, self.notes_input):
                widget.setEnabled(not self.read_only)
        elif self.user is not None and self.user.warehouse is not None:
            self.warehouse_combo.select_record(self.user.warehouse)

        self._on_party_changed()
        if self.is_new and not self.line_editor._rows:
            self.line_editor.add_row()

    def _on_party_changed(self):
        supplier = self.supplier_combo.current()
        self.line_editor.set_tax_context(self.warehouse_combo.current(), supplier)
        if supplier is not None and self.is_new and supplier.lead_time_days:
            self.eta_input.setDate(
                QDate.currentDate().addDays(int(supplier.lead_time_days))
            )

    def _fail(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        return False

    def save(self, confirm=False):
        self.error_label.setVisible(False)

        supplier = self.supplier_combo.current()
        warehouse = self.warehouse_combo.current()
        if supplier is None:
            return self._fail("Choose a supplier.")
        if warehouse is None:
            return self._fail("Choose the warehouse the goods are delivered to.")

        problem = self.line_editor.problem()
        if problem:
            return self._fail(problem)

        lines = self.line_editor.lines()
        notes = self.notes_input.toPlainText().strip() or None
        eta = self.eta_input.date().toPython()
        invoice_no = self.invoice_input.text().strip() or None

        try:
            if self.is_new:
                order = purchasing.create_purchase_order(
                    supplier, warehouse, lines, user=self.user, expected_date=eta,
                    notes=notes,
                )
            else:
                order = self.order
                order.supplier = supplier
                order.warehouse = warehouse
                order.expected_date = eta
                order.notes = notes
                order.supplier_invoice_no = invoice_no
                order.save()
                # Simplest correct path for a draft: replace the lines wholesale.
                from database.models import PurchaseOrderLine
                PurchaseOrderLine.delete().where(
                    PurchaseOrderLine.order == order
                ).execute()
                for line in lines:
                    PurchaseOrderLine.create(
                        order=order, item=line["item"], quantity=line["quantity"],
                        unit_cost=line["unit_cost"], gst_rate=line["gst_rate"],
                        hsn_code=line.get("hsn_code"),
                    )
                purchasing.recalculate_totals(order)

            if invoice_no and self.is_new:
                order.supplier_invoice_no = invoice_no
                order.save()

            if confirm:
                purchasing.confirm_order(order, self.user)

            self.saved_order = order
            self.accept()
        except Exception as exc:
            return self._fail(str(exc))


class ReceiveGoodsDialog(QDialog):
    """Book in what actually arrived against a purchase order."""

    COL_ITEM, COL_ORDERED, COL_OUTSTANDING, COL_RECEIVE, COL_COST, COL_LOT, \
        COL_EXPIRY, COL_SERIALS = range(8)

    def __init__(self, parent=None, order=None, user=None):
        super().__init__(parent)
        self.order = order
        self.user = user
        self.receipt = None

        self.setWindowTitle(f"Receive against {order.number}")
        self.setMinimumSize(1180, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel(f"Receive Goods — {order.number}")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            f"{order.supplier.name} → {order.warehouse.name}. Enter what actually "
            f"arrived. Short deliveries are fine — the order stays open for the rest. "
            f"Receiving updates each item's average cost automatically."
        ))

        header = QHBoxLayout()
        header.setSpacing(18)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd MMM yyyy")
        self.date_input.setDate(QDate.currentDate())
        self.truck_input = QLineEdit()
        self.truck_input.setPlaceholderText("e.g. MH12AB1234")
        form.addRow(common.field_label("Receipt date"), self.date_input)
        form.addRow(common.field_label("Truck HSRP"), self.truck_input)

        form2 = QFormLayout()
        form2.setVerticalSpacing(10)
        self.invoice_input = QLineEdit()
        self.invoice_input.setText(order.supplier_invoice_no or "")
        self.notes_input = QLineEdit()
        form2.addRow(common.field_label("Supplier invoice"), self.invoice_input)
        form2.addRow(common.field_label("Notes"), self.notes_input)

        header.addLayout(form)
        header.addLayout(form2)
        header.addStretch()
        layout.addLayout(header)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Item", "Ordered", "Outstanding", "Receiving", "Unit Cost", "Batch No.",
            "Expiry", "Serial Numbers",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(48)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(self.COL_ITEM, QHeaderView.Stretch)
        for col, width in ((self.COL_ORDERED, 90), (self.COL_OUTSTANDING, 110),
                           (self.COL_RECEIVE, 120), (self.COL_COST, 120),
                           (self.COL_LOT, 130), (self.COL_EXPIRY, 130),
                           (self.COL_SERIALS, 200)):
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
        buttons.addWidget(common.action_button("Receive everything outstanding",
                                               self._fill_all, "ghost"))
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Confirm Receipt", self.submit, "primary"))
        layout.addLayout(buttons)

        self._rows = []
        self._populate()

    def _static(self, text, align=Qt.AlignLeft | Qt.AlignVCenter):
        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemIsEnabled)
        cell.setTextAlignment(align)
        return cell

    def _populate(self):
        lines = purchasing.outstanding_lines(self.order)
        self.table.setRowCount(len(lines))
        right = Qt.AlignRight | Qt.AlignVCenter

        for row, line in enumerate(lines):
            item = line.item
            self.table.setItem(row, self.COL_ITEM,
                               self._static(f"{item.name}  ·  {item.sku}"))
            self.table.setItem(row, self.COL_ORDERED,
                               self._static(theme.quantity(line.quantity), right))
            self.table.setItem(row, self.COL_OUTSTANDING,
                               self._static(theme.quantity(line.outstanding), right))

            allow_decimal = bool(item.base_uom and item.base_uom.allow_decimal)
            receive = common.DecimalSpin(decimals=3 if allow_decimal else 0)
            receive.setMaximum(float(line.outstanding))
            receive.valueChanged.connect(lambda _v: self._update_summary())
            self.table.setCellWidget(row, self.COL_RECEIVE, receive)

            cost = common.DecimalSpin(decimals=2, prefix="₹ ")
            cost.setValue(float(line.unit_cost or 0))
            cost.valueChanged.connect(lambda _v: self._update_summary())
            self.table.setCellWidget(row, self.COL_COST, cost)

            lot_input = QLineEdit()
            expiry_input = QDateEdit()
            expiry_input.setCalendarPopup(True)
            expiry_input.setDisplayFormat("dd MMM yyyy")
            expiry_input.setDate(QDate.currentDate().addDays(
                int(item.shelf_life_days or 180)))
            if item.is_lot_tracked:
                lot_input.setPlaceholderText("Batch no. *")
            else:
                lot_input.setPlaceholderText("—")
                lot_input.setEnabled(False)
                expiry_input.setEnabled(False)
            self.table.setCellWidget(row, self.COL_LOT, lot_input)
            self.table.setCellWidget(row, self.COL_EXPIRY, expiry_input)

            serials_input = QLineEdit()
            if item.is_serial_tracked:
                serials_input.setPlaceholderText("Scan serials, comma separated")
            else:
                serials_input.setPlaceholderText("—")
                serials_input.setEnabled(False)
            self.table.setCellWidget(row, self.COL_SERIALS, serials_input)

            self._rows.append({
                "line": line, "item": item, "receive": receive, "cost": cost,
                "lot": lot_input, "expiry": expiry_input, "serials": serials_input,
            })

        self._update_summary()

    def _fill_all(self):
        for entry in self._rows:
            entry["receive"].setValue(float(entry["line"].outstanding))

    def _update_summary(self):
        count = 0
        value = Decimal("0")
        for entry in self._rows:
            quantity = _dec(entry["receive"].value())
            if quantity > 0:
                count += 1
                value += quantity * _dec(entry["cost"].value())
        self.summary.setText(
            f"{count} line(s) being received · goods value {theme.money(value)} "
            f"excluding GST"
        )

    def submit(self):
        self.error_label.setVisible(False)

        payload = []
        for entry in self._rows:
            quantity = _dec(entry["receive"].value())
            if quantity <= 0:
                continue
            item = entry["item"]
            serials = [s.strip() for s in entry["serials"].text().replace("\n", ",").split(",")
                       if s.strip()] if item.is_serial_tracked else []
            payload.append({
                "order_line": entry["line"],
                "quantity": quantity,
                "unit_cost": _dec(entry["cost"].value()),
                "lot_number": entry["lot"].text().strip() if item.is_lot_tracked else None,
                "expiry_date": (entry["expiry"].date().toPython()
                                if item.is_lot_tracked else None),
                "serials": serials,
            })

        if not payload:
            self.error_label.setText("Enter a received quantity on at least one line.")
            self.error_label.setVisible(True)
            return

        try:
            self.receipt = purchasing.receive_goods(
                self.order, payload, user=self.user,
                receipt_date=self.date_input.date().toPython(),
                truck_hsrp=self.truck_input.text().strip() or None,
                supplier_invoice_no=self.invoice_input.text().strip() or None,
                notes=self.notes_input.text().strip() or None,
            )
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return

        order = type(self.order).get_by_id(self.order.id)
        common.info(
            self, "Goods received",
            f"{self.receipt.number} recorded against {order.number}.",
            f"The order is now {PurchaseStatus.LABELS.get(order.status, order.status)}. "
            f"Stock and average costs have been updated.",
        )
        self.accept()


def _po_tone(status):
    return {
        PurchaseStatus.DRAFT: "neutral",
        PurchaseStatus.ORDERED: "info",
        PurchaseStatus.PARTIAL: "warning",
        PurchaseStatus.RECEIVED: "success",
        PurchaseStatus.CANCELLED: "danger",
    }.get(status, "neutral")
