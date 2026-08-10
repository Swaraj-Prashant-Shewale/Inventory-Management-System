"""Results list for the header search bar."""
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from services import search as search_service
from ui import theme
from ui.widgets import common
from ui.widgets.table import CENTER, Cell, Column, DataTable, Row, text_cell

TONE = {
    search_service.ITEM: theme.TEAL,
    search_service.SALES_ORDER: theme.INFO,
    search_service.PURCHASE_ORDER: theme.WARNING,
    search_service.CUSTOMER: theme.SUCCESS,
    search_service.SUPPLIER: theme.ACCENT,
    search_service.EMPLOYEE: theme.TEXT_MUTED,
}


class SearchResultsDialog(QDialog):
    """Shows what matched. `self.chosen` holds the selected Result on accept."""

    def __init__(self, parent=None, term="", results=None):
        super().__init__(parent)
        self.results = list(results or [])
        self.chosen = None

        self.setWindowTitle(f"Search — {term}")
        self.setMinimumSize(760, 460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        heading = QLabel(f'Results for "{term}"')
        heading.setObjectName("ScreenTitle")
        header.addWidget(heading)
        header.addStretch()
        count = QLabel(search_service.summarise(self.results)
                       or "nothing found")
        count.setObjectName("Subtle")
        header.addWidget(count)
        layout.addLayout(header)

        self.table = DataTable(
            [
                Column("Type", width=140, align=CENTER),
                Column("Name", width="stretch"),
                Column("Details", width="stretch"),
            ],
            checkable=False,
            empty_text=f"Nothing matches “{term}”.",
        )
        self.table.row_activated.connect(self._activate)
        layout.addWidget(self.table, 1)

        if not self.results:
            layout.addWidget(common.subtle(
                "Search covers item names, SKUs and barcodes, order numbers, truck "
                "numbers, customers, suppliers and employee badges."))

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Close", self.reject, "action"))
        self.open_button = common.action_button("Open", self._open_selected, "primary")
        self.open_button.setEnabled(False)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)

        self.table.selection_changed.connect(
            lambda: self.open_button.setEnabled(
                self.table.current_payload() is not None))

        self._populate()

    def _populate(self):
        rows = []
        for result in self.results:
            rows.append(Row(
                cells=[
                    Cell(result.type_label, align=CENTER,
                         colour=TONE.get(result.kind), bold=True,
                         sort_value=result.kind),
                    text_cell(result.label, bold=True,
                              tooltip="Exact match" if result.exact else None),
                    text_cell(result.detail, tone=theme.TEXT_MUTED),
                ],
                payload=result,
                tint="#F2FAFB" if result.exact else None,
            ))
        self.table.set_rows(rows)
        if rows:
            self.table.selectRow(0)

    def _activate(self, result):
        self.chosen = result
        self.accept()

    def _open_selected(self):
        result = self.table.current_payload()
        if result is not None:
            self.chosen = result
            self.accept()
