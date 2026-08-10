"""Sales order editor, fulfilment dialog and payment recording."""
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

from database.models import Customer, PaymentStatus, SalesStatus, Warehouse
from services import inventory, sales
from ui import theme
from ui.widgets import common
from ui.widgets.line_editor import SALES, LineEditor


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def so_tone(status):
    return {
        SalesStatus.DRAFT: "neutral",
        SalesStatus.CONFIRMED: "info",
        SalesStatus.PARTIAL: "warning",
        SalesStatus.SHIPPED: "success",
        SalesStatus.CANCELLED: "danger",
    }.get(status, "neutral")


def payment_tone(status):
    return {
        PaymentStatus.UNPAID: "danger",
        PaymentStatus.PARTIAL: "warning",
        PaymentStatus.PAID: "success",
    }.get(status, "neutral")


class SalesOrderDialog(QDialog):
    """Create or edit a draft sales order."""

    def __init__(self, parent=None, order=None, user=None):
        super().__init__(parent)
        self.order = order
        self.user = user
        self.is_new = order is None
        self.saved_order = None
        self.read_only = order is not None and order.status != SalesStatus.DRAFT

        title = "New Sales Order" if self.is_new else f"Sales Order {order.number}"
        self.setWindowTitle(title)
        self.setMinimumSize(1120, 680)
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
            heading_row.addWidget(common.StatusPill(
                SalesStatus.LABELS.get(order.status, order.status), so_tone(order.status)))
            heading_row.addWidget(common.StatusPill(
                PaymentStatus.LABELS.get(order.payment_status, order.payment_status),
                payment_tone(order.payment_status)))
        layout.addLayout(heading_row)

        if self.read_only:
            layout.addWidget(common.subtle(
                "This order is confirmed, so its lines are locked. Use Fulfil to ship "
                "against it."
            ))

        layout.addWidget(self._build_header_form())

        self.line_editor = LineEditor(mode=SALES, allow_edit=not self.read_only)
        layout.addWidget(self.line_editor, 1)

        self.stock_warning = QLabel("")
        self.stock_warning.setWordWrap(True)
        self.stock_warning.setStyleSheet(
            f"color: {theme.WARNING}; font-size: 13px; font-weight: 600;"
        )
        self.stock_warning.setVisible(False)
        layout.addWidget(self.stock_warning)
        self.line_editor.changed.connect(self._check_stock)

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
            buttons.addWidget(common.action_button("Save Draft", self.save, "action"))
            buttons.addWidget(common.action_button(
                "Save and Confirm", lambda: self.save(confirm=True), "primary"))
        layout.addLayout(buttons)

        self._load()

    def _build_header_form(self):
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)

        left = QFormLayout()
        left.setVerticalSpacing(10)
        self.customer_combo = common.ComboField(allow_blank=True,
                                                blank_text="— choose customer —")
        self.customer_combo.setMinimumWidth(250)
        self.customer_combo.currentIndexChanged.connect(self._on_customer_changed)
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(250)
        self.warehouse_combo.currentIndexChanged.connect(self._on_party_changed)
        left.addRow(common.field_label("Customer *"), self.customer_combo)
        left.addRow(common.field_label("Ship from *"), self.warehouse_combo)

        middle = QFormLayout()
        middle.setVerticalSpacing(10)
        self.promised_input = QDateEdit()
        self.promised_input.setCalendarPopup(True)
        self.promised_input.setDisplayFormat("dd MMM yyyy")
        self.promised_input.setDate(QDate.currentDate().addDays(3))
        self.pincode_input = QLineEdit()
        self.pincode_input.setPlaceholderText("Delivery postcode")
        middle.addRow(common.field_label("Promised date"), self.promised_input)
        middle.addRow(common.field_label("Delivery PIN"), self.pincode_input)

        right = QFormLayout()
        right.setVerticalSpacing(10)
        self.truck_input = QLineEdit()
        self.truck_input.setPlaceholderText("e.g. MH12AB1234")
        self.notes_input = QPlainTextEdit()
        self.notes_input.setFixedHeight(58)
        right.addRow(common.field_label("Truck HSRP"), self.truck_input)
        right.addRow(common.field_label("Notes"), self.notes_input)

        row.addLayout(left, 1)
        row.addLayout(middle, 1)
        row.addLayout(right, 1)
        return panel

    def _load(self):
        self.customer_combo.load(
            Customer.select().where(Customer.is_active == True).order_by(Customer.name),  # noqa: E712
            label=lambda c: f"{c.name} ({c.code})",
        )
        self.warehouse_combo.load(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name),  # noqa: E712
            label=lambda w: f"{w.name} ({w.code})",
        )

        if self.order is not None:
            self.customer_combo.select_record(self.order.customer)
            self.warehouse_combo.select_record(self.order.warehouse)
            if self.order.promised_date:
                d = self.order.promised_date
                self.promised_input.setDate(QDate(d.year, d.month, d.day))
            self.pincode_input.setText(self.order.delivery_pincode or "")
            self.truck_input.setText(self.order.truck_hsrp or "")
            self.notes_input.setPlainText(self.order.notes or "")
            self.line_editor.load_lines(list(self.order.lines))
            for widget in (self.customer_combo, self.warehouse_combo,
                           self.promised_input, self.pincode_input, self.truck_input,
                           self.notes_input):
                widget.setEnabled(not self.read_only)
        elif self.user is not None and self.user.warehouse is not None:
            self.warehouse_combo.select_record(self.user.warehouse)

        self._on_customer_changed()
        if self.is_new and not self.line_editor._rows:
            self.line_editor.add_row()

    def _on_customer_changed(self):
        customer = self.customer_combo.current()
        if customer is not None and self.is_new:
            self.pincode_input.setText(customer.delivery_pincode or "")
        self.line_editor.set_customer(customer)
        self._on_party_changed()

    def _on_party_changed(self):
        self.line_editor.set_tax_context(self.warehouse_combo.current(),
                                         self.customer_combo.current())
        self._check_stock()

    def _check_stock(self):
        """Warn about lines the warehouse cannot cover — without blocking the order."""
        warehouse = self.warehouse_combo.current()
        if warehouse is None:
            self.stock_warning.setVisible(False)
            return

        short = []
        for line in self.line_editor.lines():
            free = inventory.available(line["item"], warehouse)
            if line["quantity"] > free:
                short.append(f"{line['item'].name} (need "
                             f"{inventory.fmt_qty(line['quantity'])}, "
                             f"{inventory.fmt_qty(free)} available)")
        if short:
            self.stock_warning.setText(
                "Not enough stock for: " + "; ".join(short[:4])
                + ("…" if len(short) > 4 else "")
                + ". You can still take the order — raise a purchase order to cover it."
            )
            self.stock_warning.setVisible(True)
        else:
            self.stock_warning.setVisible(False)

    def _fail(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        return False

    def save(self, confirm=False):
        self.error_label.setVisible(False)

        customer = self.customer_combo.current()
        warehouse = self.warehouse_combo.current()
        if customer is None:
            return self._fail("Choose a customer.")
        if warehouse is None:
            return self._fail("Choose the warehouse the goods ship from.")

        problem = self.line_editor.problem()
        if problem:
            return self._fail(problem)

        lines = self.line_editor.lines()
        try:
            if self.is_new:
                order = sales.create_sales_order(
                    customer, warehouse, lines, user=self.user,
                    promised_date=self.promised_input.date().toPython(),
                    truck_hsrp=self.truck_input.text().strip() or None,
                    delivery_pincode=self.pincode_input.text().strip() or None,
                    notes=self.notes_input.toPlainText().strip() or None,
                )
            else:
                order = self.order
                order.customer = customer
                order.warehouse = warehouse
                order.promised_date = self.promised_input.date().toPython()
                order.truck_hsrp = self.truck_input.text().strip() or None
                order.delivery_pincode = self.pincode_input.text().strip() or None
                order.notes = self.notes_input.toPlainText().strip() or None
                order.save()
                from database.models import SalesOrderLine
                SalesOrderLine.delete().where(SalesOrderLine.order == order).execute()
                for line in lines:
                    SalesOrderLine.create(
                        order=order, item=line["item"], quantity=line["quantity"],
                        unit_price=line["unit_price"],
                        discount_percent=line["discount_percent"],
                        gst_rate=line["gst_rate"], hsn_code=line.get("hsn_code"),
                    )
                sales.recalculate_totals(order)

            if confirm:
                sales.confirm_order(order, self.user)
                short = sales.shortfalls(order)
                if short:
                    common.warn(
                        self, "Confirmed with a shortfall",
                        f"{order.number} is confirmed, but stock is short on "
                        f"{len(short)} line(s).",
                        "\n".join(
                            f"  • {line.item.name}: need "
                            f"{inventory.fmt_qty(line.outstanding)}, "
                            f"{inventory.fmt_qty(free)} available"
                            for line, free in short[:8]
                        ) + "\n\nRaise a purchase order from the Inventory screen.",
                    )

            self.saved_order = order
            self.accept()
        except Exception as exc:
            return self._fail(str(exc))


class FulfillDialog(QDialog):
    """Ship goods against a confirmed sales order."""

    COL_ITEM, COL_ORDERED, COL_OUTSTANDING, COL_AVAILABLE, COL_SHIP, COL_BATCH, \
        COL_SERIALS = range(7)

    def __init__(self, parent=None, order=None, user=None):
        super().__init__(parent)
        self.order = order
        self.user = user
        self.fulfillment = None

        self.setWindowTitle(f"Fulfil {order.number}")
        self.setMinimumSize(1120, 600)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel(f"Fulfil Order — {order.number}")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            f"{order.warehouse.name} → {order.customer.name}. Enter what is actually "
            f"going on the truck; a part shipment leaves the order open. Batch-tracked "
            f"items are picked earliest-expiry-first automatically."
        ))

        header = QHBoxLayout()
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd MMM yyyy")
        self.date_input.setDate(QDate.currentDate())
        self.truck_input = QLineEdit()
        self.truck_input.setText(order.truck_hsrp or "")
        self.truck_input.setPlaceholderText("e.g. MH12AB1234")
        form.addRow(common.field_label("Ship date"), self.date_input)
        form.addRow(common.field_label("Truck HSRP"), self.truck_input)
        header.addLayout(form)
        header.addStretch()
        layout.addLayout(header)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Item", "Ordered", "Outstanding", "Available", "Shipping",
            "Batch (auto FEFO)", "Serial Numbers",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(48)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(self.COL_ITEM, QHeaderView.Stretch)
        for col, width in ((self.COL_ORDERED, 90), (self.COL_OUTSTANDING, 110),
                           (self.COL_AVAILABLE, 100), (self.COL_SHIP, 120),
                           (self.COL_BATCH, 190), (self.COL_SERIALS, 200)):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addWidget(common.action_button("Ship everything outstanding",
                                               self._fill_all, "ghost"))
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Confirm Shipment", self.submit, "primary"))
        layout.addLayout(buttons)

        self._rows = []
        self._populate()

    def _static(self, text, align=Qt.AlignLeft | Qt.AlignVCenter, tone=None):
        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemIsEnabled)
        cell.setTextAlignment(align)
        if tone:
            from PySide6.QtGui import QBrush, QColor
            cell.setForeground(QBrush(QColor(tone)))
        return cell

    def _populate(self):
        lines = sales.outstanding_lines(self.order)
        self.table.setRowCount(len(lines))
        right = Qt.AlignRight | Qt.AlignVCenter

        for row, line in enumerate(lines):
            item = line.item
            free = inventory.available(item, self.order.warehouse)
            short = free < line.outstanding

            self.table.setItem(row, self.COL_ITEM,
                               self._static(f"{item.name}  ·  {item.sku}"))
            self.table.setItem(row, self.COL_ORDERED,
                               self._static(theme.quantity(line.quantity), right))
            self.table.setItem(row, self.COL_OUTSTANDING,
                               self._static(theme.quantity(line.outstanding), right))
            self.table.setItem(row, self.COL_AVAILABLE,
                               self._static(theme.quantity(free), right,
                                            theme.DANGER if short else None))

            allow_decimal = bool(item.base_uom and item.base_uom.allow_decimal)
            ship = common.DecimalSpin(decimals=3 if allow_decimal else 0)
            # Cap at what is both outstanding and physically present.
            on_hand = inventory.on_hand(item, self.order.warehouse)
            ship.setMaximum(float(min(_dec(line.outstanding), max(_dec(on_hand), Decimal(0)))))
            self.table.setCellWidget(row, self.COL_SHIP, ship)

            if item.is_lot_tracked:
                batches = inventory.available_lots(item, self.order.warehouse)
                label = ", ".join(
                    f"{ls.lot.lot_number} ({theme.quantity(ls.quantity)})"
                    for ls in batches[:2]
                ) or "no batch in stock"
                if len(batches) > 2:
                    label += f" +{len(batches) - 2}"
                batch_cell = self._static(label, tone=theme.TEXT_MUTED)
                batch_cell.setToolTip(
                    "Picked earliest-expiry-first:\n" + "\n".join(
                        f"{ls.lot.lot_number} — {theme.quantity(ls.quantity)} "
                        f"— expires {theme.date_short(ls.lot.expiry_date)}"
                        for ls in batches
                    )
                )
                self.table.setItem(row, self.COL_BATCH, batch_cell)
            else:
                self.table.setItem(row, self.COL_BATCH,
                                   self._static("—", tone=theme.TEXT_MUTED))

            serials_input = QLineEdit()
            if item.is_serial_tracked:
                serials_input.setPlaceholderText("Scan serials, comma separated")
            else:
                serials_input.setPlaceholderText("—")
                serials_input.setEnabled(False)
            self.table.setCellWidget(row, self.COL_SERIALS, serials_input)

            self._rows.append({"line": line, "item": item, "ship": ship,
                               "serials": serials_input})

    def _fill_all(self):
        for entry in self._rows:
            entry["ship"].setValue(entry["ship"].maximum())

    def submit(self):
        self.error_label.setVisible(False)

        payload = []
        for entry in self._rows:
            quantity = _dec(entry["ship"].value())
            if quantity <= 0:
                continue
            item = entry["item"]
            serials = [s.strip() for s in
                       entry["serials"].text().replace("\n", ",").split(",")
                       if s.strip()] if item.is_serial_tracked else []
            payload.append({"order_line": entry["line"], "quantity": quantity,
                            "serials": serials})

        if not payload:
            self.error_label.setText("Enter a quantity to ship on at least one line.")
            self.error_label.setVisible(True)
            return

        try:
            self.fulfillment = sales.fulfill(
                self.order, payload, user=self.user,
                ship_date=self.date_input.date().toPython(),
                truck_hsrp=self.truck_input.text().strip() or None,
            )
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return

        order = type(self.order).get_by_id(self.order.id)
        common.info(
            self, "Shipment recorded",
            f"{self.fulfillment.number} shipped against {order.number}.",
            f"The order is now {SalesStatus.LABELS.get(order.status, order.status)}.",
        )
        self.accept()


class PaymentDialog(QDialog):
    """Record a full or part payment against a sales order."""

    METHODS = ["CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD", "CREDIT NOTE"]

    def __init__(self, parent=None, order=None, user=None):
        super().__init__(parent)
        self.order = order
        self.user = user
        self.payment = None

        self.setWindowTitle(f"Record payment — {order.number}")
        self.setMinimumWidth(520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Record Payment")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        card = common.Card(padding=16)
        summary = QLabel(
            f"<b>{order.number}</b> — {order.customer.name}<br>"
            f"Order total <b>{theme.money(order.total)}</b><br>"
            f"Already paid {theme.money(order.amount_paid)}<br>"
            f"<span style='font-size:17px;'>Outstanding "
            f"<b>{theme.money(order.balance_due)}</b></span>"
        )
        summary.setTextFormat(Qt.RichText)
        card.add(summary)
        layout.addWidget(card)

        form = QFormLayout()
        form.setVerticalSpacing(12)

        self.amount_input = common.DecimalSpin(decimals=2, prefix="₹ ")
        self.amount_input.setValue(float(order.balance_due))
        self.method_combo = common.ComboField()
        self.method_combo.load_choices([(m, m.title()) for m in self.METHODS])
        self.reference_input = QLineEdit()
        self.reference_input.setPlaceholderText("UTR, cheque number, transaction ID…")
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd MMM yyyy")
        self.date_input.setDate(QDate.currentDate())
        self.notes_input = QLineEdit()
        self.overpay_check = common.checkbox("Allow more than the outstanding balance")

        form.addRow(common.field_label("Amount *"), self.amount_input)
        form.addRow(common.field_label("Method"), self.method_combo)
        form.addRow(common.field_label("Reference"), self.reference_input)
        form.addRow(common.field_label("Date"), self.date_input)
        form.addRow(common.field_label("Notes"), self.notes_input)
        form.addRow("", self.overpay_check)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Record Payment", self.submit, "primary"))
        layout.addLayout(buttons)

    def submit(self):
        self.error_label.setVisible(False)
        try:
            self.payment = sales.record_payment(
                self.order, _dec(self.amount_input.value()), user=self.user,
                method=self.method_combo.current(),
                reference=self.reference_input.text().strip() or None,
                payment_date=self.date_input.date().toPython(),
                notes=self.notes_input.text().strip() or None,
                allow_overpayment=self.overpay_check.isChecked(),
            )
            self.accept()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
