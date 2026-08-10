"""Receiving screen — IMPORTS: purchase orders through to goods receipt."""
import datetime

from peewee import prefetch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from database.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseStatus,
    Supplier,
    Warehouse,
)
from services import auth, documents, purchasing
from ui import theme
from ui.open_document import open_document as _open_document
from ui.dialogs.purchase_dialogs import PurchaseOrderDialog, ReceiveGoodsDialog
from ui.widgets import common
from ui.widgets.table import (
    CENTER,
    Cell,
    Column,
    DataTable,
    RIGHT,
    Row,
    date_cell,
    money_cell,
    text_cell,
)

ALL = "__all__"


class ReceivingScreen(QWidget):
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
                Column("Cost", width=140, align=RIGHT),
                Column("Warehouse", width="content"),
                Column("Supplier", width="stretch"),
                Column("ETA", width=130, align=CENTER),
                Column("Received", width=110, align=CENTER,
                       tooltip="How much of the order has arrived"),
            ],
            checkable=False,
            empty_text="No purchase orders yet — press Create to raise one.",
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
        row.addWidget(common.screen_title("IMPORTS"))

        refresh = common.IconButton("refresh", 34)
        refresh.setToolTip("Reload from the database")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        row.addStretch()

        can_buy = auth.can(self.user, auth.PERM_CREATE_PURCHASE)
        can_receive = auth.can(self.user, auth.PERM_RECEIVE_GOODS)

        self.create_button = common.action_button("Create", self._create, "action")
        self.create_button.setEnabled(can_buy)

        self.receive_button = common.action_button(
            "Receive", self._receive, "primary",
            tooltip="Book in what actually arrived against this order.")
        self.receive_button.setEnabled(False)

        self.confirm_button = common.action_button(
            "Send to Supplier", self._confirm, "action",
            tooltip="Move a draft order to Ordered.")
        self.confirm_button.setEnabled(False)

        self.cancel_button = common.action_button("Cancel Order", self._cancel, "action")
        self.cancel_button.setEnabled(False)

        self.delete_button = common.action_button("Delete", self._delete, "danger")
        self.delete_button.setEnabled(False)

        self.print_button = common.action_button("Print", self._print, "action")
        self.print_button.setEnabled(False)

        if not can_receive:
            self.receive_button.setToolTip("Your role cannot receive goods.")

        for button in (self.create_button, self.receive_button, self.confirm_button,
                       self.cancel_button, self.delete_button, self.print_button):
            row.addWidget(button)
        return row

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        row.addWidget(common.field_label("Warehouse"))
        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(190)
        self.warehouse_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.warehouse_combo)

        row.addWidget(common.field_label("Supplier"))
        self.supplier_combo = common.ComboField()
        self.supplier_combo.setMinimumWidth(190)
        self.supplier_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.supplier_combo)

        row.addWidget(common.field_label("Status"))
        self.status_combo = common.ComboField()
        self.status_combo.setMinimumWidth(170)
        self.status_combo.load_choices(
            [(ALL, "All statuses"), ("__open__", "Open only")]
            + [(s, PurchaseStatus.LABELS[s]) for s in
               (PurchaseStatus.DRAFT, PurchaseStatus.ORDERED, PurchaseStatus.PARTIAL,
                PurchaseStatus.RECEIVED, PurchaseStatus.CANCELLED)]
        )
        self.status_combo.setCurrentIndex(1)
        self.status_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.status_combo)

        self.overdue_check = common.checkbox("Overdue only")
        self.overdue_check.toggled.connect(self.refresh)
        row.addWidget(self.overdue_check)

        row.addStretch()
        self.export_button = common.action_button("Export", self._export, "action")
        row.addWidget(self.export_button)
        return row

    def _load_filters(self):
        for combo, model, blank in (
            (self.warehouse_combo, Warehouse, "All warehouses"),
            (self.supplier_combo, Supplier, "All suppliers"),
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
        self._load_filters_if_needed()
        query = PurchaseOrder.select()

        warehouse = self.warehouse_combo.currentData()
        if warehouse not in (None, ALL):
            query = query.where(PurchaseOrder.warehouse == warehouse)
        supplier = self.supplier_combo.currentData()
        if supplier not in (None, ALL):
            query = query.where(PurchaseOrder.supplier == supplier)

        status = self.status_combo.current()
        if status == "__open__":
            query = query.where(PurchaseOrder.status.in_(PurchaseStatus.OPEN))
        elif status not in (None, ALL):
            query = query.where(PurchaseOrder.status == status)

        if self.overdue_check.isChecked():
            query = query.where(
                (PurchaseOrder.status.in_(PurchaseStatus.OPEN))
                & (PurchaseOrder.expected_date.is_null(False))
                & (PurchaseOrder.expected_date < datetime.date.today())
            )

        # Join supplier and warehouse, and prefetch the lines in a single extra query:
        # the naive version issued one query per order per column and crawled remotely.
        query = (
            query.select(PurchaseOrder, Supplier, Warehouse)
            .join(Supplier).switch(PurchaseOrder).join(Warehouse)
            .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc())
        )
        orders = prefetch(query, PurchaseOrderLine)

        rows = []
        total_value = 0
        overdue = 0
        for order in orders:
            if self._search:
                haystack = f"{order.number} {order.supplier.name} " \
                           f"{order.supplier_invoice_no or ''}".lower()
                if self._search not in haystack:
                    continue

            lines = list(order.lines)
            ordered = sum((line.quantity for line in lines), 0)
            received = sum((line.received_quantity for line in lines), 0)
            progress = f"{(received / ordered * 100):.0f}%" if ordered else "—"

            is_overdue = order.is_overdue
            overdue += is_overdue
            total_value += float(order.total or 0)

            eta = date_cell(order.expected_date)
            if is_overdue:
                eta = Cell(theme.date_short(order.expected_date), align=CENTER,
                           sort_value=order.expected_date, colour=theme.DANGER, bold=True,
                           tooltip="Past its expected date")

            rows.append(Row(
                cells=[
                    text_cell(order.number, bold=True),
                    Cell(PurchaseStatus.LABELS.get(order.status, order.status),
                         align=CENTER, sort_value=order.status,
                         colour=_status_colour(order.status)),
                    money_cell(order.total),
                    text_cell(order.warehouse.name),
                    text_cell(order.supplier.name),
                    eta,
                    Cell(progress, align=CENTER, sort_value=received),
                ],
                payload=order,
                tint="#FFF6F5" if is_overdue else None,
            ))

        self.table.set_rows(rows)
        parts = [f"{len(rows)} order(s)", f"value {theme.money(total_value)}"]
        if overdue:
            parts.append(f"{overdue} overdue")
        self.status_label.setText("  ·  ".join(parts))
        self._update_buttons()

    def _load_filters_if_needed(self):
        if self.warehouse_combo.count() <= 1:
            self._load_filters()

    # --- Actions --------------------------------------------------------------------

    def _update_buttons(self):
        order = self.table.current_payload()
        exists = order is not None
        status = order.status if exists else None

        self.print_button.setEnabled(exists)
        self.receive_button.setEnabled(
            exists and status in (PurchaseStatus.ORDERED, PurchaseStatus.PARTIAL)
            and auth.can(self.user, auth.PERM_RECEIVE_GOODS)
        )
        self.confirm_button.setEnabled(
            exists and status == PurchaseStatus.DRAFT
            and auth.can(self.user, auth.PERM_CREATE_PURCHASE)
        )
        self.cancel_button.setEnabled(
            exists and status in PurchaseStatus.OPEN
            and auth.can(self.user, auth.PERM_CREATE_PURCHASE)
        )
        self.delete_button.setEnabled(
            exists and status == PurchaseStatus.DRAFT
            and auth.can(self.user, auth.PERM_DELETE_RECORDS)
        )

    def _create(self):
        dialog = PurchaseOrderDialog(self, order=None, user=self.user)
        if dialog.exec():
            self.refresh()

    def _open_order(self, order):
        dialog = PurchaseOrderDialog(self, order=order, user=self.user)
        if dialog.exec():
            self.refresh()

    def _receive(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not purchasing.outstanding_lines(order):
            common.info(self, "Nothing outstanding",
                        f"Every line on {order.number} has already been received.")
            return
        dialog = ReceiveGoodsDialog(self, order=order, user=self.user)
        if dialog.exec():
            self.refresh()

    def _confirm(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not common.confirm(
            self, "Send to supplier?",
            f"Mark {order.number} as ordered?",
            f"{order.supplier.name} · {theme.money(order.total)}\n\n"
            f"Its lines will be locked after this. You can still receive against it "
            f"or cancel it.",
            confirm_label="Send order",
        ):
            return
        try:
            purchasing.confirm_order(order, self.user)
        except Exception as exc:
            common.error(self, "Could not send the order", str(exc))
            return
        self.refresh()

    def _cancel(self):
        order = self.table.current_payload()
        if order is None:
            return
        if not common.confirm(
            self, "Cancel order?", f"Cancel {order.number}?",
            "Anything already received stays in stock. The order can't be reopened.",
            confirm_label="Cancel order", destructive=True,
        ):
            return
        try:
            purchasing.cancel_order(order, self.user)
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
            purchasing.delete_order(order, self.user)
        except Exception as exc:
            common.error(self, "Could not delete", str(exc))
            return
        self.refresh()

    def _print(self):
        order = self.table.current_payload()
        if order is None:
            return

        receipts = list(order.receipts)
        choices = [("po", f"Purchase order {order.number}")]
        choices += [("grn:%d" % r.id, f"Goods receipt note {r.number}")
                    for r in receipts]

        choice = common.choose_from_menu(self, self.print_button, choices)
        if choice is None:
            return

        try:
            if choice == "po":
                path = documents.purchase_order(order)
            else:
                receipt_id = int(choice.split(":", 1)[1])
                receipt = next(r for r in receipts if r.id == receipt_id)
                path = documents.goods_receipt_note(receipt)
        except Exception as exc:
            common.error(self, "Could not create the PDF", str(exc))
            return
        _open_document(self, path)

    def _export(self):
        if not self.table.row_count():
            common.info(self, "Nothing to export", "There are no rows in the table.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export purchase orders", "purchase_orders",
            "Excel workbook (*.xlsx);;CSV file (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                self.table.export_csv(path if path.lower().endswith(".csv")
                                      else path + ".csv", title="Purchase Orders")
            else:
                self.table.export_xlsx(path if path.lower().endswith(".xlsx")
                                       else path + ".xlsx", title="Purchase Orders")
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))

    def _context_menu(self, position):
        order = self.table.current_payload()
        if order is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction("Open order…")
        receive_action = menu.addAction("Receive goods…")
        receive_action.setEnabled(self.receive_button.isEnabled())
        menu.addSeparator()
        history = menu.addAction("View receipts…")

        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is open_action:
            self._open_order(order)
        elif chosen is receive_action:
            self._receive()
        elif chosen is history:
            self._show_receipts(order)

    def _show_receipts(self, order):
        receipts = list(order.receipts)
        if not receipts:
            common.info(self, "No receipts yet",
                        f"Nothing has been booked in against {order.number}.")
            return
        detail = []
        for receipt in receipts:
            detail.append(
                f"{receipt.number} — {theme.date_short(receipt.receipt_date)}"
                + (f" — truck {receipt.truck_hsrp}" if receipt.truck_hsrp else "")
            )
            for line in receipt.lines:
                detail.append(
                    f"     {line.item.name}: {theme.quantity(line.quantity)} "
                    f"@ {theme.money(line.unit_cost)}"
                    + (f" — batch {line.lot.lot_number}" if line.lot else "")
                )
        common.info(self, f"Receipts against {order.number}",
                    f"{len(receipts)} receipt(s):", "\n".join(detail))


def _status_colour(status):
    return {
        PurchaseStatus.DRAFT: theme.TEXT_MUTED,
        PurchaseStatus.ORDERED: theme.INFO,
        PurchaseStatus.PARTIAL: theme.WARNING,
        PurchaseStatus.RECEIVED: theme.SUCCESS,
        PurchaseStatus.CANCELLED: theme.DANGER,
    }.get(status)
