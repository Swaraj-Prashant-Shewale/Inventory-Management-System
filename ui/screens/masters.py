"""Generic master-data list tab: search, table, add / edit / delete, export.

Every master record (warehouses, categories, suppliers, customers, price lists,
employees, users) is the same interaction, so they share this one widget and differ only
in their column and form definitions.
"""
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from database.connection import db
from services import auth
from ui.widgets import common
from ui.widgets.table import DataTable, Row


class MasterListTab(QWidget):
    def __init__(
        self,
        parent,
        user,
        *,
        title,
        singular,
        columns,
        query_fn,
        row_fn,
        spec_fn,
        save_fn,
        permission,
        delete_permission=auth.PERM_DELETE_RECORDS,
        delete_guard=None,
        search_placeholder="Search…",
        extra_buttons=None,
        empty_text=None,
        intro=None,
    ):
        super().__init__(parent)
        self.user = user
        self.title = title
        self.singular = singular
        self.query_fn = query_fn
        self.row_fn = row_fn
        self.spec_fn = spec_fn
        self.save_fn = save_fn
        self.permission = permission
        self.delete_permission = delete_permission
        self.delete_guard = delete_guard
        self._search = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        if intro:
            layout.addWidget(common.subtle(intro))

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(search_placeholder)
        self.search_input.setMaximumWidth(300)
        self.search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search_input)
        toolbar.addStretch()

        can_manage = auth.can(user, permission)
        self.add_button = common.action_button(f"New {singular}", self.add, "primary")
        self.add_button.setEnabled(can_manage)
        self.edit_button = common.action_button("Edit", self.edit, "action")
        self.edit_button.setEnabled(False)
        self.delete_button = common.action_button("Delete", self.delete, "danger")
        self.delete_button.setEnabled(False)
        self.export_button = common.action_button("Export", self.export, "action")

        if not can_manage:
            self.add_button.setToolTip("Your role cannot create these records.")

        for button in (self.add_button, self.edit_button, self.delete_button):
            toolbar.addWidget(button)
        for extra in (extra_buttons or []):
            toolbar.addWidget(extra)
        toolbar.addWidget(self.export_button)
        layout.addLayout(toolbar)

        self.table = DataTable(
            columns, checkable=False,
            empty_text=empty_text or f"No {title.lower()} yet.",
        )
        self.table.selection_changed.connect(self._update_buttons)
        self.table.row_activated.connect(lambda record: self.edit(record))
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtle")
        layout.addWidget(self.status_label)

        self.refresh()

    # --- Data -----------------------------------------------------------------------

    def _on_search(self, text):
        self._search = (text or "").strip().lower()
        self.refresh()

    def refresh(self):
        records = list(self.query_fn())
        rows = []
        for record in records:
            cells = self.row_fn(record)
            if self._search:
                haystack = " ".join(
                    str(getattr(c, "text", c) or "") for c in cells
                ).lower()
                if self._search not in haystack:
                    continue
            rows.append(Row(cells=cells, payload=record))
        self.table.set_rows(rows)
        self.status_label.setText(f"{len(rows)} of {len(records)} {self.title.lower()}")
        self._update_buttons()

    def _update_buttons(self):
        record = self.table.current_payload()
        can_manage = auth.can(self.user, self.permission)
        self.edit_button.setEnabled(record is not None and can_manage)
        self.delete_button.setEnabled(
            record is not None and auth.can(self.user, self.delete_permission)
        )

    # --- Actions --------------------------------------------------------------------

    def add(self):
        if not auth.can(self.user, self.permission):
            return
        from ui.dialogs.record_dialog import RecordDialog
        dialog = RecordDialog(self, self.spec_fn(None), instance=None,
                              save_fn=self.save_fn)
        if dialog.exec():
            self.refresh()
            return dialog.saved_record
        return None

    def edit(self, record=None):
        record = record if record is not None else self.table.current_payload()
        if record is None:
            return
        read_only = not auth.can(self.user, self.permission)
        from ui.dialogs.record_dialog import RecordDialog
        dialog = RecordDialog(self, self.spec_fn(record), instance=record,
                              save_fn=self.save_fn, read_only=read_only)
        if dialog.exec():
            self.refresh()

    def delete(self):
        record = self.table.current_payload()
        if record is None:
            return
        if not auth.can(self.user, self.delete_permission):
            common.warn(self, "Not permitted", "Only an Owner can delete records.")
            return

        label = getattr(record, "name", None) or getattr(record, "full_name", str(record))

        if self.delete_guard is not None:
            problem = self.delete_guard(record)
            if problem:
                common.warn(self, f"Cannot delete {label}", problem)
                return

        if not common.confirm(
            self, f"Delete {self.singular.lower()}?",
            f"Permanently delete '{label}'?",
            "This cannot be undone. Marking the record inactive is usually safer, as it "
            "keeps it out of dropdowns while preserving its history.",
            confirm_label="Delete permanently", destructive=True,
        ):
            return

        try:
            with db.atomic():
                record.delete_instance()
            auth.record_audit(self.user, "DELETE", type(record).__name__, None,
                              f"Deleted {self.singular.lower()} {label}")
        except Exception as exc:
            common.error(
                self, "Could not delete", f"'{label}' is still referenced elsewhere.",
                f"{exc}\n\nMark it inactive instead.",
            )
            return
        self.refresh()

    def export(self):
        if not self.table.row_count():
            common.info(self, "Nothing to export", "There are no rows in the table.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, f"Export {self.title}", self.title.lower().replace(" ", "_"),
            "Excel workbook (*.xlsx);;CSV file (*.csv)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in selected:
                if not path.lower().endswith(".csv"):
                    path += ".csv"
                self.table.export_csv(path, title=self.title)
            else:
                if not path.lower().endswith(".xlsx"):
                    path += ".xlsx"
                self.table.export_xlsx(path, title=self.title)
            common.info(self, "Exported", f"Saved to:\n{path}")
        except Exception as exc:
            common.error(self, "Export failed", str(exc))
