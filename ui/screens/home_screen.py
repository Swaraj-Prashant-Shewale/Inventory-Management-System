"""Home — the daily overview from the design PDF.

Reminders and the highest-profit items on the left; the employee roster on the right,
with badge clock-in. Everything reads live data; nothing here is decorative.
"""
import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from database.models import (
    Employee,
    Reminder,
    ReminderSource,
    TaskStatus,
    Warehouse,
)
from services import alerts, analytics, auth, workforce
from ui import theme
from ui.widgets import common
from ui.widgets.charts import ChartCard, EmptyChart, horizontal_bar_chart
from ui.widgets.table import CENTER, Cell, Column, DataTable, RIGHT, Row, text_cell

ALL = "__all__"

SOURCE_TONE = {
    ReminderSource.MANUAL: theme.ACCENT,
    ReminderSource.LOW_STOCK: theme.DANGER,
    ReminderSource.PO_OVERDUE: theme.WARNING,
    ReminderSource.LOT_EXPIRY: theme.INFO,
}


class HomeScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(16)

        layout.addWidget(self._build_sidebar(), 32)
        layout.addWidget(self._build_main(), 68)

        self.refresh()

    # --- Left column ----------------------------------------------------------------

    def _build_sidebar(self):
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        # Reminders
        self.reminders_card = common.Card(padding=16)
        header = QHBoxLayout()
        header.addWidget(common.section_title("Reminders"))
        header.addStretch()
        self.reminder_count = QLabel("")
        self.reminder_count.setObjectName("Subtle")
        header.addWidget(self.reminder_count)
        add = common.action_button("+", self._add_reminder, "ghost")
        add.setObjectName("AddCircle")
        add.setToolTip("Add a reminder")
        header.addWidget(add)
        self.reminders_card.add_layout(header)

        self.reminders_scroll = QScrollArea()
        self.reminders_scroll.setWidgetResizable(True)
        self.reminders_scroll.setFrameShape(QScrollArea.NoFrame)
        self.reminders_scroll.setMinimumHeight(240)
        self.reminders_card.add(self.reminders_scroll, 1)
        column.addWidget(self.reminders_card, 1)

        # Highest profit items
        self.profit_card = common.Card(padding=16)
        profit_header = QHBoxLayout()
        profit_header.addWidget(common.section_title("Highest Profit Items"))
        profit_header.addStretch()
        self.profit_period = common.ComboField()
        self.profit_period.setMinimumWidth(140)
        self.profit_period.load_choices(analytics.PERIOD_CHOICES,
                                        selected=analytics.THIS_MONTH)
        self.profit_period.currentIndexChanged.connect(self._refresh_profit)
        profit_header.addWidget(self.profit_period)
        self.profit_card.add_layout(profit_header)

        self.profit_holder = QVBoxLayout()
        self.profit_card.add_layout(self.profit_holder, 1)
        column.addWidget(self.profit_card, 1)

        return panel

    # --- Right column ---------------------------------------------------------------

    def _build_main(self):
        card = common.Card(padding=18)

        header = QHBoxLayout()
        header.addWidget(common.section_title("Employees"))
        header.addStretch()

        self.on_shift_label = QLabel("")
        self.on_shift_label.setObjectName("Subtle")
        header.addWidget(self.on_shift_label)

        self.warehouse_combo = common.ComboField()
        self.warehouse_combo.setMinimumWidth(170)
        self.warehouse_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.warehouse_combo)

        self.clock_button = common.action_button("Clock In / Out", self._clock,
                                                 "primary")
        self.clock_button.setToolTip("Scan an ID badge to clock someone in or out")
        header.addWidget(self.clock_button)

        self.task_button = common.action_button("Assign Task", self._assign_task,
                                                "action")
        self.task_button.setEnabled(auth.can(self.user, auth.PERM_MANAGE_TASKS))
        if not self.task_button.isEnabled():
            self.task_button.setToolTip("Your role cannot assign tasks.")
        header.addWidget(self.task_button)
        card.add_layout(header)

        self.table = DataTable(
            [
                Column("Name", width="stretch"),
                Column("Task", width="stretch"),
                Column("Hours", width=110, align=RIGHT,
                       tooltip="Hours worked today, from badge scans"),
                Column("Warehouse", width="content"),
                Column("Status", width=110, align=CENTER),
            ],
            checkable=False,
            empty_text="No employees yet — add them under Settings → Employees.",
        )
        self.table.row_activated.connect(self._open_employee)
        card.add(self.table, 1)
        return card

    def _load_filters(self):
        self.warehouse_combo.blockSignals(True)
        self.warehouse_combo.clear()
        self.warehouse_combo.addItem("All warehouses", ALL)
        for warehouse in Warehouse.select().where(
            Warehouse.is_active == True  # noqa: E712
        ).order_by(Warehouse.name):
            self.warehouse_combo.addItem(warehouse.name, warehouse)
        if self.user.warehouse is not None:
            self.warehouse_combo.select_record(self.user.warehouse)
        self.warehouse_combo.blockSignals(False)

    def _current_warehouse(self):
        value = self.warehouse_combo.currentData()
        return None if value in (None, ALL) else value

    # --- Refresh --------------------------------------------------------------------

    def refresh(self):
        if self.warehouse_combo.count() <= 1:
            self._load_filters()
        self._refresh_reminders()
        self._refresh_profit()
        self._refresh_roster()

    def _refresh_reminders(self):
        reminders = alerts.active_reminders(limit=40)
        counts = alerts.reminder_counts()
        automatic = counts["total"] - counts.get(ReminderSource.MANUAL, 0)
        self.reminder_count.setText(
            f"{counts['total']} open" + (f" · {automatic} automatic" if automatic else "")
        )

        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(8)

        today = datetime.date.today()
        if not reminders:
            column.addWidget(common.subtle(
                "Nothing outstanding. Low stock, late purchase orders and expiring "
                "batches appear here automatically."))
        for reminder in reminders:
            column.addWidget(self._reminder_row(reminder, today))
        column.addStretch()

        previous = self.reminders_scroll.takeWidget()
        if previous is not None:
            previous.deleteLater()
        self.reminders_scroll.setWidget(body)

    def _reminder_row(self, reminder, today):
        overdue = reminder.due_date and reminder.due_date < today
        tone = SOURCE_TONE.get(reminder.source, theme.ACCENT)

        row = QWidget()
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setStyleSheet(
            f"background-color: {'#FFF6F5' if overdue else '#F9FAFA'};"
            f" border-radius: 6px; border-left: 4px solid {tone};"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 6, 6, 6)
        layout.setSpacing(6)

        text = QVBoxLayout()
        text.setSpacing(1)
        when = QLabel(theme.date_short(reminder.due_date)
                      + ("  ·  overdue" if overdue else "")
                      + (f"  ·  {ReminderSource.LABELS.get(reminder.source)}"
                         if reminder.source != ReminderSource.MANUAL else ""))
        when.setStyleSheet(
            f"color: {theme.DANGER if overdue else theme.TEXT_MUTED}; font-size: 10px;"
            f" font-weight: 700; background: transparent; border: none;")
        title = QLabel(reminder.title)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 600;"
            f" background: transparent; border: none;")
        text.addWidget(when)
        text.addWidget(title)
        if reminder.details:
            title.setToolTip(reminder.details)

        done = common.action_button("✓", lambda _=False, r=reminder: self._complete(r),
                                    "ghost")
        done.setToolTip("Mark as done")
        done.setFixedWidth(28)
        done.setStyleSheet(
            f"color: {theme.SUCCESS}; font-weight: 700; border: none;"
            f" background: transparent;")

        layout.addLayout(text, 1)
        layout.addWidget(done)
        return row

    def _refresh_profit(self):
        while self.profit_holder.count():
            child = self.profit_holder.takeAt(0)
            if child.widget():
                child.widget().setParent(None)

        period = self.profit_period.current() or analytics.THIS_MONTH
        start, end, _ = analytics.resolve_period(period)
        warehouse = self._current_warehouse()
        top = analytics.top_items_by_profit(start, end, limit=4, warehouse=warehouse)

        if not auth.can(self.user, auth.PERM_VIEW_COST):
            self.profit_holder.addWidget(EmptyChart(
                "Profit figures are hidden for your role."))
            return
        if not top:
            self.profit_holder.addWidget(EmptyChart(
                "No profit recorded in this period yet."))
            return

        chart = horizontal_bar_chart(
            [{"label": entry["item"].name, "value": entry["value"]} for entry in top],
            value_formatter=lambda v: theme.money(v),
        )
        self.profit_holder.addWidget(ChartCard("", chart, height=170))

    def _refresh_roster(self):
        warehouse = self._current_warehouse()
        rows = []
        on_shift = 0

        for entry in workforce.roster(warehouse):
            employee = entry["employee"]
            on_shift += entry["clocked_in"]
            task = entry["task"]

            task_label = task.title if task else "—"
            if task and entry["open_tasks"] > 1:
                task_label += f"  (+{entry['open_tasks'] - 1} more)"

            rows.append(Row(
                cells=[
                    text_cell(employee.name, bold=True,
                              tooltip=f"Badge {employee.code}"),
                    Cell(task_label,
                         colour=theme.TEXT if task else theme.TEXT_MUTED,
                         tooltip=(task.details or task.title) if task else None,
                         sort_value=task.title if task else ""),
                    Cell(f"{entry['hours_today']:.2f}", align=RIGHT,
                         sort_value=entry["hours_today"],
                         colour=theme.TEXT_MUTED if not entry["hours_today"] else None),
                    text_cell(entry["warehouse"].name if entry["warehouse"] else "—"),
                    Cell("On shift" if entry["clocked_in"] else "Off",
                         align=CENTER, bold=entry["clocked_in"],
                         colour=theme.SUCCESS if entry["clocked_in"] else theme.TEXT_MUTED,
                         sort_value=0 if entry["clocked_in"] else 1),
                ],
                payload=employee,
            ))

        self.table.set_rows(rows)
        self.on_shift_label.setText(
            f"{on_shift} of {len(rows)} on shift  ·  hours are for today"
        )

    # --- Actions --------------------------------------------------------------------

    def _add_reminder(self):
        dialog = ReminderDialog(self, user=self.user)
        if dialog.exec():
            self._refresh_reminders()

    def _complete(self, reminder):
        reminder.is_done = True
        reminder.save()
        self._refresh_reminders()

    def _clock(self):
        dialog = ClockDialog(self, user=self.user,
                             warehouse=self._current_warehouse())
        dialog.exec()
        self._refresh_roster()

    def _assign_task(self):
        if not auth.can(self.user, auth.PERM_MANAGE_TASKS):
            return
        dialog = TaskDialog(self, user=self.user,
                            employee=self.table.current_payload())
        if dialog.exec():
            self._refresh_roster()

    def _open_employee(self, employee):
        tasks = list(employee.tasks.where(
            employee.tasks.model.status.in_(TaskStatus.ACTIVE)))
        detail = [f"Badge {employee.code}",
                  f"Designation: {employee.designation or '—'}",
                  f"Warehouse: {employee.warehouse.name if employee.warehouse else '—'}",
                  f"Currently: {'on shift' if workforce.is_clocked_in(employee) else 'off'}",
                  f"Hours today: {workforce.hours_worked(employee, *workforce.day_bounds()):.2f}",
                  f"Hours this week: {workforce.hours_worked(employee, *workforce.week_bounds()):.2f}",
                  ""]
        if tasks:
            detail.append("Open tasks:")
            detail += [f"  • {t.title}"
                       f" ({TaskStatus.LABELS.get(t.status, t.status)})" for t in tasks]
        else:
            detail.append("No open tasks.")
        common.info(self, employee.name, employee.designation or "Employee",
                    "\n".join(detail))


class ReminderDialog(QDialog):
    """Add a reminder by hand. Automatic ones are generated, not typed."""

    def __init__(self, parent=None, user=None):
        super().__init__(parent)
        self.user = user
        self.setWindowTitle("Add Reminder")
        self.setMinimumWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Add Reminder")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("What needs doing")
        self.details_input = QPlainTextEdit()
        self.details_input.setFixedHeight(70)
        from PySide6.QtWidgets import QDateEdit
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd MMM yyyy")
        self.date_input.setDate(QDate.currentDate())
        form.addRow(common.field_label("Reminder *"), self.title_input)
        form.addRow(common.field_label("Due"), self.date_input)
        form.addRow(common.field_label("Details"), self.details_input)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Add", self.submit, "primary"))
        layout.addLayout(buttons)

    def submit(self):
        title = self.title_input.text().strip()
        if not title:
            self.error_label.setText("Enter what the reminder is for.")
            self.error_label.setVisible(True)
            return
        Reminder.create(
            title=title, details=self.details_input.toPlainText().strip() or None,
            due_date=self.date_input.date().toPython(),
            source=ReminderSource.MANUAL, created_by=self.user,
        )
        self.accept()


class ClockDialog(QDialog):
    """Badge scanner panel: scan to toggle in or out, one after another."""

    def __init__(self, parent=None, user=None, warehouse=None):
        super().__init__(parent)
        self.user = user
        self.warehouse = warehouse
        self.setWindowTitle("Clock In / Out")
        self.setMinimumWidth(480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Clock In / Out")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            "Scan an ID badge, or type the badge code and press Enter. Each scan "
            "flips that person between on shift and off. The box stays ready for the "
            "next person."))

        self.input = QLineEdit()
        self.input.setPlaceholderText("Scan badge…")
        self.input.setMinimumHeight(44)
        self.input.returnPressed.connect(self.submit)
        layout.addWidget(self.input)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setMinimumHeight(52)
        self.result.setAlignment(Qt.AlignCenter)
        self.result.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {theme.TEXT_MUTED};")
        layout.addWidget(self.result)

        self.history = QLabel("")
        self.history.setObjectName("Subtle")
        self.history.setWordWrap(True)
        layout.addWidget(self.history)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Done", self.accept, "primary"))
        layout.addLayout(buttons)

        self._log = []
        self.input.setFocus()

    def submit(self):
        code = self.input.text().strip()
        if not code:
            return
        try:
            employee, entry_type = workforce.clock_scan(
                code, warehouse=self.warehouse, user=self.user)
        except Exception as exc:
            self.result.setText(str(exc))
            self.result.setStyleSheet(
                f"font-size: 14px; font-weight: 600; color: {theme.DANGER};")
            self.input.selectAll()
            return

        clocked_in = entry_type == workforce.IN
        self.result.setText(
            f"{employee.name} clocked {'IN' if clocked_in else 'OUT'}"
        )
        self.result.setStyleSheet(
            f"font-size: 17px; font-weight: 700;"
            f" color: {theme.SUCCESS if clocked_in else theme.INFO};")

        hours = workforce.hours_worked(employee, *workforce.day_bounds())
        self._log.insert(0, f"{datetime.datetime.now():%H:%M}  {employee.name} "
                             f"{'in' if clocked_in else 'out'} ({hours:.2f} h today)")
        self.history.setText("\n".join(self._log[:6]))

        self.input.clear()
        self.input.setFocus()


class TaskDialog(QDialog):
    """Assign a task to someone on the floor."""

    def __init__(self, parent=None, user=None, employee=None):
        super().__init__(parent)
        self.user = user
        self.setWindowTitle("Assign Task")
        self.setMinimumWidth(480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Assign Task")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        form = QFormLayout()
        form.setVerticalSpacing(12)

        self.employee_combo = common.ComboField(allow_blank=True,
                                                blank_text="— choose employee —")
        self.employee_combo.load(
            Employee.select().where(Employee.is_active == True).order_by(Employee.name),  # noqa: E712
            label=lambda e: f"{e.name} ({e.code})", selected=employee)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("e.g. Pick order SO/26-27/0042")
        self.details_input = QPlainTextEdit()
        self.details_input.setFixedHeight(66)

        from PySide6.QtWidgets import QDateEdit
        self.due_input = QDateEdit()
        self.due_input.setCalendarPopup(True)
        self.due_input.setDisplayFormat("dd MMM yyyy")
        self.due_input.setDate(QDate.currentDate())

        self.priority_combo = common.ComboField()
        self.priority_combo.load_choices([(1, "High"), (2, "Normal"), (3, "Low")],
                                         selected=2)

        form.addRow(common.field_label("Employee *"), self.employee_combo)
        form.addRow(common.field_label("Task *"), self.title_input)
        form.addRow(common.field_label("Due"), self.due_input)
        form.addRow(common.field_label("Priority"), self.priority_combo)
        form.addRow(common.field_label("Details"), self.details_input)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Assign", self.submit, "primary"))
        layout.addLayout(buttons)

    def submit(self):
        self.error_label.setVisible(False)
        try:
            workforce.assign_task(
                self.title_input.text(), self.employee_combo.current(),
                assigned_by=self.user,
                details=self.details_input.toPlainText().strip() or None,
                due_date=self.due_input.date().toPython(),
                priority=self.priority_combo.current() or 2,
            )
            self.accept()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
