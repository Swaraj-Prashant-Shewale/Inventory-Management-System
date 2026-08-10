"""Operations tabs that sit alongside the stock list on the Inventory screen.

The design PDF only draws the stock table; adjustments, transfers and counts are the
machinery that keeps that table true, so they live beside it as sibling tabs.
"""
from decimal import Decimal

from peewee import JOIN, prefetch
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTabWidget, QVBoxLayout, QWidget

from database.models import (
    AdjustmentReason,
    CountStatus,
    CycleCount,
    CycleCountLine,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
    TransferStatus,
    User,
    Warehouse,
)
from services import auth, warehouse_ops
from ui import theme
from ui.dialogs.operations_dialogs import AdjustmentDialog, CycleCountDialog, TransferDialog
from ui.screens.inventory_screen import InventoryScreen
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


class InventoryArea(QWidget):
    """Container: the PDF's stock table plus the operations that maintain it."""

    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.stock = InventoryScreen(user)
        self.adjustments = AdjustmentsTab(user)
        self.transfers = TransfersTab(user)
        self.counts = CycleCountsTab(user)

        self.tabs.addTab(self.stock, "Stock")
        self.tabs.addTab(self.adjustments, "Adjustments")
        self.tabs.addTab(self.transfers, "Transfers")
        self.tabs.addTab(self.counts, "Cycle Counts")
        self.tabs.currentChanged.connect(lambda _i: self.refresh())
        layout.addWidget(self.tabs)

    def refresh(self):
        widget = self.tabs.currentWidget()
        if hasattr(widget, "refresh"):
            widget.refresh()

    def set_search_text(self, text):
        self.tabs.setCurrentIndex(0)
        self.stock.set_search_text(text)

    # Proxies so the main window can keep talking to the stock screen.
    @property
    def table(self):
        return self.stock.table

    def _edit_item(self, item):
        self.tabs.setCurrentIndex(0)
        self.stock._edit_item(item)


class _OpsTab(QWidget):
    """Shared scaffolding: toolbar, table, status line."""

    def __init__(self, user, title, columns, empty_text, parent=None):
        super().__init__(parent)
        self.user = user

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        self.toolbar = QHBoxLayout()
        self.toolbar.setSpacing(8)
        self.toolbar.addWidget(common.section_title(title))
        self.toolbar.addStretch()
        layout.addLayout(self.toolbar)

        self.table = DataTable(columns, checkable=False, empty_text=empty_text)
        self.table.selection_changed.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtle")
        layout.addWidget(self.status_label)

    def _update_buttons(self):
        pass


class AdjustmentsTab(_OpsTab):
    def __init__(self, user, parent=None):
        super().__init__(
            user, "Stock Adjustments",
            [
                Column("Number", width=150),
                Column("Date", width=130, align=CENTER),
                Column("Warehouse", width="content"),
                Column("Reason", width=140),
                Column("Lines", width=80, align=RIGHT),
                Column("Value", width=130, align=RIGHT),
                Column("Status", width=170, align=CENTER),
                Column("Raised By", width="stretch"),
            ],
            "No stock adjustments recorded.", parent)

        self.new_button = common.action_button("New Adjustment", self._create, "primary")
        self.new_button.setEnabled(auth.can(user, auth.PERM_ADJUST_STOCK))
        self.approve_button = common.action_button("Approve", self._approve, "action")
        self.approve_button.setEnabled(False)
        self.toolbar.addWidget(self.new_button)
        self.toolbar.addWidget(self.approve_button)
        self.refresh()

    def refresh(self):
        query = (
            StockAdjustment.select(StockAdjustment, Warehouse, User)
            .join(Warehouse).switch(StockAdjustment)
            .join(User, JOIN.LEFT_OUTER, on=StockAdjustment.created_by)
            .order_by(StockAdjustment.id.desc())
        )
        rows = []
        pending = 0
        for adjustment in prefetch(query, StockAdjustmentLine):
            approved = adjustment.approved_at is not None
            pending += not approved
            lines = list(adjustment.lines)
            value = sum((Decimal(str(l.quantity_delta or 0))
                         * Decimal(str(l.unit_cost or 0)) for l in lines), Decimal("0"))
            rows.append(Row(
                cells=[
                    text_cell(adjustment.number, bold=True),
                    date_cell(adjustment.adjustment_date),
                    text_cell(adjustment.warehouse.name),
                    text_cell(AdjustmentReason.LABELS.get(adjustment.reason,
                                                          adjustment.reason)),
                    text_cell(str(len(lines)), align=RIGHT),
                    money_cell(value, tone=theme.DANGER if value < 0 else theme.SUCCESS),
                    Cell("Approved" if approved else "Awaiting approval", align=CENTER,
                         colour=theme.SUCCESS if approved else theme.WARNING,
                         sort_value=1 if approved else 0),
                    text_cell(adjustment.created_by.full_name
                              if adjustment.created_by else "—"),
                ],
                payload=adjustment,
                tint=None if approved else "#FFFBF2",
            ))
        self.table.set_rows(rows)
        self.status_label.setText(
            f"{len(rows)} adjustment(s)"
            + (f"  ·  {pending} awaiting approval" if pending else "")
        )
        self._update_buttons()

    def _update_buttons(self):
        adjustment = self.table.current_payload()
        self.approve_button.setEnabled(
            adjustment is not None and adjustment.approved_at is None
            and auth.can(self.user, auth.PERM_APPROVE_ADJUSTMENT)
        )

    def _create(self):
        dialog = AdjustmentDialog(self, user=self.user)
        if dialog.exec():
            self.refresh()

    def _approve(self):
        adjustment = self.table.current_payload()
        if adjustment is None:
            return
        detail = "\n".join(
            f"  • {line.item.name}: "
            f"{'+' if line.quantity_delta > 0 else ''}"
            f"{theme.quantity(line.quantity_delta)}"
            for line in adjustment.lines
        )
        if not common.confirm(
            self, "Approve adjustment?",
            f"Approve {adjustment.number} and move the stock?",
            f"{detail}\n\nNet value {theme.money(warehouse_ops.adjustment_value(adjustment))}"
            f"\n\nThis writes to the stock ledger and cannot be undone — you would need "
            f"a second adjustment to reverse it.",
            confirm_label="Approve and post",
        ):
            return
        try:
            warehouse_ops.approve_adjustment(adjustment, self.user)
        except Exception as exc:
            common.error(self, "Could not approve", str(exc))
            return
        self.refresh()


class TransfersTab(_OpsTab):
    def __init__(self, user, parent=None):
        super().__init__(
            user, "Warehouse Transfers",
            [
                Column("Number", width=150),
                Column("From", width="content"),
                Column("To", width="content"),
                Column("Lines", width=80, align=RIGHT),
                Column("Status", width=140, align=CENTER),
                Column("Dispatched", width=130, align=CENTER),
                Column("Received", width=130, align=CENTER),
                Column("Truck HSRP", width="stretch", align=CENTER),
            ],
            "No transfers recorded.", parent)

        can_transfer = auth.can(user, auth.PERM_TRANSFER_STOCK)
        self.new_button = common.action_button("New Transfer", self._create, "primary")
        self.new_button.setEnabled(can_transfer)
        self.dispatch_button = common.action_button("Dispatch", self._dispatch, "action")
        self.dispatch_button.setEnabled(False)
        self.receive_button = common.action_button("Receive", self._receive, "action")
        self.receive_button.setEnabled(False)
        for button in (self.new_button, self.dispatch_button, self.receive_button):
            self.toolbar.addWidget(button)
        self.refresh()

    def refresh(self):
        source = Warehouse.alias()
        destination = Warehouse.alias()
        query = (
            StockTransfer.select(StockTransfer, source, destination)
            .join(source, on=(StockTransfer.from_warehouse == source.id))
            .switch(StockTransfer)
            .join(destination, on=(StockTransfer.to_warehouse == destination.id))
            .order_by(StockTransfer.id.desc())
        )
        rows = []
        in_transit = 0
        for transfer in prefetch(query, StockTransferLine):
            in_transit += transfer.status == TransferStatus.IN_TRANSIT
            rows.append(Row(
                cells=[
                    text_cell(transfer.number, bold=True),
                    text_cell(transfer.from_warehouse.name),
                    text_cell(transfer.to_warehouse.name),
                    text_cell(str(len(list(transfer.lines))), align=RIGHT),
                    Cell(TransferStatus.LABELS.get(transfer.status, transfer.status),
                         align=CENTER, sort_value=transfer.status,
                         colour=_transfer_colour(transfer.status)),
                    date_cell(transfer.dispatch_date),
                    date_cell(transfer.received_date),
                    text_cell(transfer.truck_hsrp or "—", align=CENTER),
                ],
                payload=transfer,
                tint="#FFFBF2" if transfer.status == TransferStatus.IN_TRANSIT else None,
            ))
        self.table.set_rows(rows)
        parts = [f"{len(rows)} transfer(s)"]
        if in_transit:
            parts.append(f"{in_transit} in transit worth "
                         f"{theme.money(warehouse_ops.in_transit_value())}")
        self.status_label.setText("  ·  ".join(parts))
        self._update_buttons()

    def _update_buttons(self):
        transfer = self.table.current_payload()
        can = auth.can(self.user, auth.PERM_TRANSFER_STOCK)
        self.dispatch_button.setEnabled(
            transfer is not None and transfer.status == TransferStatus.DRAFT and can)
        self.receive_button.setEnabled(
            transfer is not None and transfer.status == TransferStatus.IN_TRANSIT and can)

    def _create(self):
        dialog = TransferDialog(self, user=self.user)
        if dialog.exec():
            self.refresh()

    def _dispatch(self):
        transfer = self.table.current_payload()
        if transfer is None:
            return
        if not common.confirm(
            self, "Dispatch transfer?",
            f"Send {transfer.number} from {transfer.from_warehouse.name}?",
            "Stock leaves the source warehouse now and stays in transit until it is "
            "received at the destination.",
            confirm_label="Dispatch",
        ):
            return
        try:
            warehouse_ops.dispatch_transfer(transfer, self.user)
        except Exception as exc:
            common.error(self, "Could not dispatch", str(exc))
            return
        self.refresh()

    def _receive(self):
        transfer = self.table.current_payload()
        if transfer is None:
            return
        if not common.confirm(
            self, "Receive transfer?",
            f"Book {transfer.number} in at {transfer.to_warehouse.name}?",
            "This receives everything that was sent. If some of it did not arrive, tell "
            "me and I can add a short-receipt screen.",
            confirm_label="Receive all",
        ):
            return
        try:
            warehouse_ops.receive_transfer(transfer, user=self.user)
        except Exception as exc:
            common.error(self, "Could not receive", str(exc))
            return
        self.refresh()


class CycleCountsTab(_OpsTab):
    def __init__(self, user, parent=None):
        super().__init__(
            user, "Cycle Counts",
            [
                Column("Number", width=150),
                Column("Date", width=130, align=CENTER),
                Column("Warehouse", width="content"),
                Column("Items", width=80, align=RIGHT),
                Column("Counted", width=100, align=RIGHT),
                Column("Variances", width=110, align=RIGHT),
                Column("Status", width=130, align=CENTER),
                Column("Counted By", width="stretch"),
            ],
            "No cycle counts yet — start one to check the shelves against the system.",
            parent)

        can_count = auth.can(user, auth.PERM_CYCLE_COUNT)
        self.new_button = common.action_button("Start Count", self._create, "primary")
        self.new_button.setEnabled(can_count)
        self.open_button = common.action_button("Enter Counts", self._open, "action")
        self.open_button.setEnabled(False)
        self.toolbar.addWidget(self.new_button)
        self.toolbar.addWidget(self.open_button)
        self.refresh()

    def refresh(self):
        query = (
            CycleCount.select(CycleCount, Warehouse, User)
            .join(Warehouse).switch(CycleCount)
            .join(User, JOIN.LEFT_OUTER, on=CycleCount.counted_by)
            .order_by(CycleCount.id.desc())
        )
        rows = []
        for count in prefetch(query, CycleCountLine):
            lines = list(count.lines)
            counted = sum(1 for line in lines if line.counted_quantity is not None)
            variances = sum(
                1 for line in lines
                if line.counted_quantity is not None
                and Decimal(str(line.counted_quantity))
                != Decimal(str(line.expected_quantity or 0))
            )
            rows.append(Row(
                cells=[
                    text_cell(count.number, bold=True),
                    date_cell(count.count_date),
                    text_cell(count.warehouse.name),
                    text_cell(str(len(lines)), align=RIGHT),
                    text_cell(f"{counted}", align=RIGHT),
                    Cell(str(variances), align=RIGHT, sort_value=variances,
                         colour=theme.WARNING if variances else theme.TEXT_MUTED),
                    Cell(CountStatus.LABELS.get(count.status, count.status),
                         align=CENTER, sort_value=count.status,
                         colour=_count_colour(count.status)),
                    text_cell(count.counted_by.full_name if count.counted_by else "—"),
                ],
                payload=count,
                tint="#FFFBF2" if count.status in (CountStatus.OPEN,
                                                   CountStatus.COUNTED) else None,
            ))
        self.table.set_rows(rows)
        self.status_label.setText(f"{len(rows)} count(s)")
        self._update_buttons()

    def _update_buttons(self):
        count = self.table.current_payload()
        self.open_button.setEnabled(
            count is not None and count.status in (CountStatus.OPEN, CountStatus.COUNTED)
            and auth.can(self.user, auth.PERM_CYCLE_COUNT)
        )

    def _create(self):
        warehouses = list(
            Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name))  # noqa: E712
        if not warehouses:
            common.warn(self, "No warehouses", "Create a warehouse first.")
            return

        warehouse = self.user.warehouse or warehouses[0]
        if not common.confirm(
            self, "Start a cycle count?",
            f"Open a count covering every stocked item at {warehouse.name}?",
            "The system snapshots what it currently believes is on the shelf. You then "
            "enter what you actually counted, and approving posts the differences.",
            confirm_label="Start count",
        ):
            return
        try:
            count = warehouse_ops.open_cycle_count(warehouse, user=self.user)
        except Exception as exc:
            common.error(self, "Could not start the count", str(exc))
            return
        self.refresh()
        dialog = CycleCountDialog(self, count=count, user=self.user)
        dialog.exec()
        self.refresh()

    def _open(self):
        count = self.table.current_payload()
        if count is None:
            return
        dialog = CycleCountDialog(self, count=count, user=self.user)
        dialog.exec()
        self.refresh()


def _transfer_colour(status):
    return {
        TransferStatus.DRAFT: theme.TEXT_MUTED,
        TransferStatus.IN_TRANSIT: theme.WARNING,
        TransferStatus.RECEIVED: theme.SUCCESS,
        TransferStatus.CANCELLED: theme.DANGER,
    }.get(status)


def _count_colour(status):
    return {
        CountStatus.OPEN: theme.WARNING,
        CountStatus.COUNTED: theme.INFO,
        CountStatus.APPROVED: theme.SUCCESS,
        CountStatus.CANCELLED: theme.DANGER,
    }.get(status)
