"""Shipping screen — EXPORTS: sales orders through fulfilment, invoicing and payment."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from database.models import Customer, PaymentStatus, SalesOrder, SalesStatus, Warehouse
from services import auth, documents, sales
from ui import theme
from ui.open_document import open_document as _open_document
from ui.dialogs.sales_dialogs import (
    FulfillDialog,
    PaymentDialog,
    SalesOrderDialog,
)
from ui.widgets import common
from ui.widgets.table import (
    CENTER,
    Cell,
    Column,
    DataTable,
    RIGHT,
    Row,
    money_cell,
    text_cell,
)

ALL = "__all__"


class ShippingScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user
        self._search = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        layout.addLayout(self._build_toolbar())
        layout.addLayout(self._build_filters())

        self.table = DataTable(
            [
                Column("Order ID", width=150),
                Column("Status", width=150, align=CENTER),
                Column("Amount", width=140, align=RIGHT),
                Column("Paid", width=130, align=CENTER),
                Column("Warehouse", width="content"),
                Column("Customer", width="stretch"),
                Column("Delivery Postcode", width=150, align=CENTER),
                Column("Truck HSRP", width=140, align=CENTER),
            ],
            checkable=False,
            empty_text="No sales orders yet — press Create to raise one.",
        )
        self.table.selection_changed.connect(self._update_buttons)
        self.table.row_activated.connect(self._open_order)
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
        row.addWidget(common.screen_title("EXPORTS"))

        refresh = common.IconButton("refresh", 34)
        refresh.setToolTip("Reload from the database")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        row.addStretch()

        can_sell = auth.can(self.user, auth.PERM_CREATE_SALES)

        self.create_button = common.action_button("Create", self._create, "action")
        self.create_button.setEnabled(can_sell)

        self.fulfill_button = common.action_button(
            "Fulfil", self._fulfill, "primary",
            tooltip="Ship goods against this order.")
        self.fulfill_button.setEnabled(False)

        self.confirm_button = common.action_button("Confirm", self._confirm, "action")
        self.confirm_button.setEnabled(False)

        self.invoice_button = common.action_button("Invoice", self._invoice, "action")
        self.invoice_button.setEnabled(False)

        self.payment_button = common.action_button("Payment", self._payment, "action")
        self.payment_button.setEnabled(False)

        self.cancel_button = common.action_button("Cancel Order", self._cancel, "action")
        self.cancel_button.setEnabled(False)

        self.delete_button = common.action_button("Delete", self._delete, "danger")
        self.delete_button.setEnabled(False)

        self.print_button = common.action_button("Print", self._print, "action")
        self.print_button.setEnabled(False)

        for button in (self.create_button, self.confirm_button, self.fulfill_button,
                       self.invoice_button, self.payment_button, self.cancel_button,
                       self.delete_button, self.print_button):
            row.addWidget(button)
        return row

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        row.addWidget(common.field_label("Warehouse"))
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(180)
        self.warehouse_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.warehouse_combo)

        row.addWidget(common.field_label("Customer"))
        self.customer_combo = common.ComboField()
        self.customer_combo.setMinimumWidth(180)
        self.customer_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.customer_combo)

        row.addWidget(common.field_label("Status"))
        self.status_combo = common.ComboField()
        self.status_combo.setMinimumWidth(160)
        self.status_combo.load_choices(
            [(ALL, "All statuses"), ("__open__", "Open only")]
            + [(s, SalesStatus.LABELS[s]) for s in
               (SalesStatus.DRAFT, SalesStatus.CONFIRMED, SalesStatus.PARTIAL,
                SalesStatus.SHIPPED, SalesStatus.CANCELLED)]
        )
        self.status_combo.setCurrentIndex(1)
        self.status_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.status_combo)

        self.unpaid_check = common.checkbox("Unpaid only")
        self.unpaid_check.toggled.connect(self.refresh)
        row.addWidget(self.unpaid_check)

        row.addStretch()
        row.addWidget(common.action_button("Export", self._export, "action"))
        return row

    def _load_filters(self):
        for combo, model, blank in (
            (self.warehouse_combo, Warehouse, "All warehouses"),
            (self.customer_combo, Customer, "All customers"),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(blank, ALL)
            for record in model.select().where(model.is_active == True).order_by(model.name):  # noqa: E712
                combo.addItem(record.name, record)
            combo.blockSignals(False)

    # --- Data -----------------------------------------------------------------------

    def set_search_text(self, text):
        self._search = (text or "").strip().lower()
        self.refresh()

    def refresh(self):
        if self.warehouse_combo.count() <= 1:
            self._load_filters()

        query = SalesOrder.select()
        warehouse = self.warehouse_combo.currentData()
        if warehouse not in (None, ALL):
            query = query.where(SalesOrder.warehouse == warehouse)
        customer = self.customer_combo.currentData()
        if customer not in (None, ALL):
            query = query.where(SalesOrder.customer == customer)

        status = self.status_combo.current()
        if status == "__open__":
            query = query.where(SalesOrder.status.in_(SalesStatus.OPEN))
        elif status not in (None, ALL):
            query = query.where(SalesOrder.status == status)

        if self.unpaid_check.isChecked():
            query = query.where(
                (SalesOrder.payment_status != PaymentStatus.PAID)
                & (SalesOrder.status != SalesStatus.CANCELLED)
            )

        # Join customer and warehouse so each row needs no follow-up queries.
        query = (
            query.select(SalesOrder, Customer, Warehouse)
            .join(Customer).switch(SalesOrder).join(Warehouse)
        )

        rows = []
        total_value = 0
        outstanding = 0
        for order in query.order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc()):
            if self._search:
                haystack = f"{order.number} {order.customer.name} " \
                           f"{order.truck_hsrp or ''}".lower()
                if self._search not in haystack:
                    continue

            total_value += float(order.total or 0)
            if order.status != SalesStatus.CANCELLED:
                outstanding += float(order.balance_due or 0)

            paid_label = PaymentStatus.LABELS.get(order.payment_status,
                                                  order.payment_status)
            if order.payment_status == PaymentStatus.PARTIAL:
                paid_label = f"{theme.money(order.amount_paid)}"

            rows.append(Row(
                cells=[
                    text_cell(order.number, bold=True),
                    Cell(SalesStatus.LABELS.get(order.status, order.status),
                         align=CENTER, sort_value=order.status,
                         colour=_status_colour(order.status)),
                    money_cell(order.total),
                    Cell(paid_label, align=CENTER, sort_value=order.amount_paid,
                         colour=_payment_colour(order.payment_status),
                         tooltip=f"Outstanding {theme.money(order.balance_due)}"),
                    text_cell(order.warehouse.name),
                    text_cell(order.customer.name),
                    text_cell(order.delivery_pincode or "—", align=CENTER),
                    text_cell(order.truck_hsrp or "—", align=CENTER),
                ],
                payload=order,
            ))

        self.table.set_rows(rows)
        self.status_label.setText(
            f"{len(rows)} order(s)  ·  value {theme.money(total_value)}  ·  "
            f"outstanding {theme.money(outstanding)}"
        )
        self._update_buttons()

    # --- Actions --------------------------------------------------------------------

    def _update_buttons(self):
        order = self.table.current_payload()
        exists = order is not None
        status = order.status if exists else None

        self.print_button.setEnabled(exists)
        self.confirm_button.setEnabled(
            exists and status == SalesStatus.DRAFT
            and auth.can(self.user, auth.PERM_CREATE_SALES))
        self.fulfill_button.setEnabled(
            exists and status in (SalesStatus.CONFIRMED, SalesStatus.PARTIAL)
            and auth.can(self.user, auth.PERM_FULFILL_GOODS))
        self.invoice_button.setEnabled(
            exists and status in (SalesStatus.CONFIRMED, SalesStatus.PARTIAL,
                                  SalesStatus.SHIPPED))
        self.payment_button.setEnabled(
            exists and status != SalesStatus.CANCELLED
            and order.payment_status != PaymentStatus.PAID
            and auth.can(self.user, auth.PERM_RECORD_PAYMENT))
        self.cancel_button.setEnabled(
            exists and status in SalesStatus.OPEN
            and auth.can(self.user, auth.PERM_CREATE_SALES))
        self.delete_button.setEnabled(
            exists and status == SalesStatus.DRAFT
            and auth.can(self.user, auth.PERM_DELETE_RECORDS))

    def _create(self):
        dialog = SalesOrderDialog(self, order=None, user=self.user)
        if dialog.exec():
            self.refresh()

    def _open_order(self, order):
        dialog = SalesOrderDialog(self, order=order, user=self.user)
        if dialog.exec():
            self.refresh()

    def _confirm(self):
        order = self.table.current_payload()
        if order is None:
            return
        short = []
        try:
            sales.confirm_order(order, self.user)
            short = sales.shortfalls(order)
        except Exception as exc:
            common.error(self, "Could not confirm", str(exc))
            return
        if short:
            common.warn(
                self, "Confirmed with a shortfall",
                f"{order.number} is confirmed, but stock is short on {len(short)} line(s).",
                "\n".join(f"  • {line.item.name}: need {line.outstanding}, "
                          f"{free} available" for line, free in short[:8]),
            )
        self.refresh()

    def _fulfill(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not sales.outstanding_lines(order):
            common.info(self, "Nothing outstanding",
                        f"Every line on {order.number} has already shipped.")
            return
        dialog = FulfillDialog(self, order=order, user=self.user)
        if dialog.exec():
            self.refresh()

    def _invoice(self):
        order = self.table.current_payload()
        if order is None:
            return
        existing = sales.invoice_for(order)
        if existing is not None and not common.confirm(
            self, "Invoice already raised",
            f"{order.number} already has invoice {existing.number}.",
            f"Dated {theme.date_short(existing.invoice_date)} for "
            f"{theme.money(existing.total)}.\n\nRaise another one?",
            confirm_label="Raise another",
        ):
            return
        try:
            invoice = sales.create_invoice(order, self.user)
        except Exception as exc:
            common.error(self, "Could not raise the invoice", str(exc))
            return

        split = (f"IGST {theme.money(invoice.igst)}" if invoice.is_interstate
                 else f"CGST {theme.money(invoice.cgst)} + "
                      f"SGST {theme.money(invoice.sgst)}")
        common.info(
            self, "Invoice raised", f"{invoice.number} for {order.customer.name}",
            f"Taxable value {theme.money(invoice.subtotal)}\n"
            f"{split}\n"
            f"Round off {theme.money(invoice.round_off)}\n"
            f"Total {theme.money(invoice.total)}\n"
            f"Due {theme.date_short(invoice.due_date)}\n\n"
            f"The printable GST invoice PDF arrives in Phase 3.",
        )
        self.refresh()

    def _payment(self):
        order = self.table.current_payload()
        if order is None:
            return
        dialog = PaymentDialog(self, order=order, user=self.user)
        if dialog.exec():
            self.refresh()

    def _cancel(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not common.confirm(
            self, "Cancel order?", f"Cancel {order.number}?",
            "Reserved stock is released. Anything already shipped stays shipped.",
            confirm_label="Cancel order", destructive=True,
        ):
            return
        try:
            sales.cancel_order(order, self.user)
        except Exception as exc:
            common.error(self, "Could not cancel", str(exc))
            return
        self.refresh()

    def _delete(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not common.confirm(
            self, "Delete draft?", f"Permanently delete draft {order.number}?",
            "This cannot be undone.", confirm_label="Delete", destructive=True,
        ):
            return
        try:
            sales.delete_order(order, self.user)
        except Exception as exc:
            common.error(self, "Could not delete", str(exc))
            return
        self.refresh()

    def _print(self):
        order = self.table.current_payload()
        if order is None:
            return

        invoice = sales.invoice_for(order)
        shipments = list(order.fulfillments)

        choices = []
        if invoice is not None:
            choices.append(("invoice", f"Tax invoice {invoice.number}"))
        choices += [(f"challan:{f.id}", f"Delivery challan {f.number}")
                    for f in shipments]

        if not choices:
            common.info(
                self, "Nothing to print yet",
                f"{order.number} has no invoice or shipment.",
                "Raise the invoice, or fulfil the order to produce a delivery challan.")
            return

        choice = common.choose_from_menu(self, self.print_button, choices)
        if choice is None:
            return

        try:
            if choice == "invoice":
                gaps = documents.company_gaps()
                if gaps and not common.confirm(
                    self, "Company details incomplete",
                    f"Your {', '.join(gaps)} " +
                    ("is" if len(gaps) == 1 else "are") + " not set.",
                    "The PDF will print, but it will be marked as NOT A VALID TAX "
                    "INVOICE. Fill in Settings → Company and reissue.\n\nPrint anyway?",
                    confirm_label="Print anyway",
                ):
                    return
                path = documents.tax_invoice(invoice)
            else:
                fulfillment_id = int(choice.split(":", 1)[1])
                fulfillment = next(f for f in shipments if f.id == fulfillment_id)
                path = documents.delivery_challan(fulfillment)
        except Exception as exc:
            common.error(self, "Could not create the PDF", str(exc))
            return
        _open_document(self, path)

    def _export(self):
        if not self.table.row_count():
            common.info(self, "Nothing to export", "There are no rows in the table.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export sales orders", "sales_orders",
            "Excel workbook (*.xlsx);;CSV file (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                self.table.export_csv(path if path.lower().endswith(".csv")
                                      else path + ".csv", title="Sales Orders")
            else:
                self.table.export_xlsx(path if path.lower().endswith(".xlsx")
                                       else path + ".xlsx", title="Sales Orders")
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))

    def _context_menu(self, position):
        order = self.table.current_payload()
        if order is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction("Open order…")
        fulfil_action = menu.addAction("Fulfil…")
        fulfil_action.setEnabled(self.fulfill_button.isEnabled())
        payment_action = menu.addAction("Record payment…")
        payment_action.setEnabled(self.payment_button.isEnabled())
        menu.addSeparator()
        shipments = menu.addAction("View shipments…")

        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is open_action:
            self._open_order(order)
        elif chosen is fulfil_action:
            self._fulfill()
        elif chosen is payment_action:
            self._payment()
        elif chosen is shipments:
            self._show_shipments(order)

    def _show_shipments(self, order):
        shipments = list(order.fulfillments)
        if not shipments:
            common.info(self, "No shipments yet",
                        f"Nothing has shipped against {order.number}.")
            return
        detail = []
        for shipment in shipments:
            detail.append(
                f"{shipment.number} — {theme.date_short(shipment.ship_date)}"
                + (f" — truck {shipment.truck_hsrp}" if shipment.truck_hsrp else "")
            )
            for line in shipment.lines:
                detail.append(
                    f"     {line.item.name}: {theme.quantity(line.quantity)} "
                    f"@ {theme.money(line.unit_price)} "
                    f"(cost {theme.money(line.unit_cost)})"
                    + (f" — batch {line.lot.lot_number}" if line.lot else "")
                )
        common.info(self, f"Shipments for {order.number}",
                    f"{len(shipments)} shipment(s):", "\n".join(detail))


def _status_colour(status):
    return {
        SalesStatus.DRAFT: theme.TEXT_MUTED,
        SalesStatus.CONFIRMED: theme.INFO,
        SalesStatus.PARTIAL: theme.WARNING,
        SalesStatus.SHIPPED: theme.SUCCESS,
        SalesStatus.CANCELLED: theme.DANGER,
    }.get(status)


def _payment_colour(status):
    return {
        PaymentStatus.UNPAID: theme.DANGER,
        PaymentStatus.PARTIAL: theme.WARNING,
        PaymentStatus.PAID: theme.SUCCESS,
    }.get(status)
