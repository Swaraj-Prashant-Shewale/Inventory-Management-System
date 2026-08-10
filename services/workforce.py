"""Employees on the floor: badge clock-in, hours worked and assigned tasks.

Clocking is a toggle driven by a badge scan — the employee's code arrives from a barcode
reader as if it were typed, and whichever state they are in flips. Hours are derived
from the IN/OUT pairs rather than stored, so a corrected entry immediately corrects the
totals.
"""
import datetime
from decimal import Decimal

from peewee import JOIN, fn

from database.connection import db
from database.models import (
    Employee,
    Task,
    TaskStatus,
    TimeEntry,
    Warehouse,
)
from services import auth

IN = "IN"
OUT = "OUT"


class WorkforceError(Exception):
    """A clocking or task operation was refused. Safe to show the user."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


# --- Clocking -----------------------------------------------------------------------

def find_by_badge(code) -> Employee:
    code = (code or "").strip()
    if not code:
        raise WorkforceError("Scan a badge or type an employee code.")
    employee = Employee.get_or_none(
        (fn.UPPER(Employee.code) == code.upper()) & (Employee.is_active == True)  # noqa: E712
    )
    if employee is None:
        raise WorkforceError(
            f"No active employee has the badge code '{code}'. "
            f"Check the Employees list under Settings."
        )
    return employee


def last_entry(employee):
    return (TimeEntry.select()
            .where(TimeEntry.employee == employee)
            .order_by(TimeEntry.timestamp.desc(), TimeEntry.id.desc())
            .first())


def is_clocked_in(employee) -> bool:
    entry = last_entry(employee)
    return entry is not None and entry.entry_type == IN


def clock_scan(code, warehouse=None, user=None, method="SCAN"):
    """Toggle a badge between clocked in and clocked out.

    Returns (employee, entry_type). The warehouse defaults to the employee's own.
    """
    employee = find_by_badge(code)
    entry_type = OUT if is_clocked_in(employee) else IN

    with db.atomic():
        TimeEntry.create(
            employee=employee,
            entry_type=entry_type,
            warehouse=warehouse or employee.warehouse,
            method=method,
            recorded_by=user,
        )

    auth.record_audit(user, "CLOCK_" + entry_type, "Employee", employee.id,
                      f"{employee.name} clocked {entry_type.lower()}")
    return employee, entry_type


def correct_entry(employee, entry_type, timestamp, user=None, note=None):
    """Add a manual entry — for the badge left at home, or a forgotten clock-out.

    A supervisor action, distinct from the badge-scan kiosk path: manual entries with an
    arbitrary timestamp move payroll hours, so it is gated (unlike clock_scan, which is a
    physical badge presentation at a terminal).
    """
    auth.require(user, auth.PERM_MANAGE_EMPLOYEES, "correct a time entry")
    if entry_type not in (IN, OUT):
        raise WorkforceError("An entry must be either IN or OUT.")
    entry = TimeEntry.create(
        employee=employee, entry_type=entry_type, timestamp=timestamp,
        warehouse=employee.warehouse, method="MANUAL", recorded_by=user, note=note,
    )
    auth.record_audit(user, "CLOCK_MANUAL", "Employee", employee.id,
                      f"Manual {entry_type} for {employee.name} at {timestamp:%d %b %H:%M}")
    return entry


def hours_worked(employee, start=None, end=None, entries=None) -> Decimal:
    """Total hours from paired IN/OUT entries.

    An unclosed IN counts up to now (never past it), so someone still on shift shows the
    hours worked so far. Callers commonly pass end=day_end (today 23:59:59); accruing an
    open shift to that would show ~15 hours at 11am, so the open shift is always capped at
    the present moment.
    """
    if entries is None:
        query = TimeEntry.select().where(TimeEntry.employee == employee)
        if start is not None:
            query = query.where(TimeEntry.timestamp >= start)
        if end is not None:
            query = query.where(TimeEntry.timestamp <= end)
        entries = list(query.order_by(TimeEntry.timestamp, TimeEntry.id))

    total = datetime.timedelta()
    open_since = None
    for entry in entries:
        if entry.entry_type == IN:
            open_since = entry.timestamp
        elif open_since is not None:
            total += entry.timestamp - open_since
            open_since = None

    if open_since is not None:
        now = datetime.datetime.now()
        accrue_to = min(end, now) if end is not None else now
        if accrue_to > open_since:
            total += accrue_to - open_since

    return (Decimal(total.total_seconds()) / Decimal(3600)).quantize(Decimal("0.01"))


def day_bounds(day=None):
    day = day or datetime.date.today()
    return (datetime.datetime.combine(day, datetime.time.min),
            datetime.datetime.combine(day, datetime.time.max))


def week_bounds(day=None):
    day = day or datetime.date.today()
    monday = day - datetime.timedelta(days=day.weekday())
    return (datetime.datetime.combine(monday, datetime.time.min),
            datetime.datetime.combine(day, datetime.time.max))


# --- Tasks --------------------------------------------------------------------------

def assign_task(title, employee, assigned_by=None, warehouse=None, details=None,
                due_date=None, priority=2) -> Task:
    auth.require(assigned_by, auth.PERM_MANAGE_TASKS, "assign tasks")
    if not (title or "").strip():
        raise WorkforceError("Give the task a title.")
    if employee is None:
        raise WorkforceError("Choose who the task is for.")

    task = Task.create(
        title=title.strip(), details=details, assigned_to=employee,
        assigned_by=assigned_by, warehouse=warehouse or employee.warehouse,
        due_date=due_date, priority=priority, status=TaskStatus.PENDING,
    )
    auth.record_audit(assigned_by, "ASSIGN", "Task", task.id,
                      f"Assigned '{task.title}' to {employee.name}")
    return task


def set_task_status(task, status, user=None) -> Task:
    auth.require(user, auth.PERM_MANAGE_TASKS, "change a task's status")
    if status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.DONE,
                      TaskStatus.CANCELLED):
        raise WorkforceError(f"Unknown task status '{status}'.")

    task.status = status
    now = datetime.datetime.now()
    if status == TaskStatus.IN_PROGRESS and task.started_at is None:
        task.started_at = now
    if status == TaskStatus.DONE:
        task.completed_at = now
    task.save()
    auth.record_audit(user, "TASK_" + status, "Task", task.id,
                      f"'{task.title}' set to {TaskStatus.LABELS.get(status, status)}")
    return task


def current_task(employee, tasks=None):
    """The task shown against a person: in-progress first, then the nearest due."""
    if tasks is not None:
        candidates = [t for t in tasks if t.status in TaskStatus.ACTIVE]
    else:
        candidates = list(Task.select().where(
            (Task.assigned_to == employee) & (Task.status.in_(TaskStatus.ACTIVE))
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (
        0 if t.status == TaskStatus.IN_PROGRESS else 1,
        t.priority or 2,
        t.due_date or datetime.date.max,
    ))
    return candidates[0]


# --- Roster -------------------------------------------------------------------------

def roster(warehouse=None, day=None):
    """Home-screen roster: each employee with their task, hours today and status.

    Built from three queries regardless of headcount — the per-employee version cost a
    round trip per row, which is ruinous over a remote database.
    """
    day_start, day_end = day_bounds(day)

    query = (Employee.select(Employee, Warehouse)
             .join(Warehouse, JOIN.LEFT_OUTER)
             .where(Employee.is_active == True))  # noqa: E712
    if warehouse is not None:
        query = query.where(Employee.warehouse == warehouse)
    employees = list(query.order_by(Employee.name))
    if not employees:
        return []

    ids = [e.id for e in employees]

    entries_by_employee = {employee_id: [] for employee_id in ids}
    for entry in (TimeEntry.select()
                  .where((TimeEntry.employee.in_(ids))
                         & (TimeEntry.timestamp >= day_start)
                         & (TimeEntry.timestamp <= day_end))
                  .order_by(TimeEntry.timestamp, TimeEntry.id)):
        entries_by_employee[entry.employee_id].append(entry)

    # The last entry overall decides who is currently on shift, even if it predates today.
    latest = {}
    for entry in (TimeEntry.select()
                  .where(TimeEntry.employee.in_(ids))
                  .order_by(TimeEntry.timestamp, TimeEntry.id)):
        latest[entry.employee_id] = entry

    tasks_by_employee = {employee_id: [] for employee_id in ids}
    for task in Task.select().where(
        (Task.assigned_to.in_(ids)) & (Task.status.in_(TaskStatus.ACTIVE))
    ):
        tasks_by_employee[task.assigned_to_id].append(task)

    result = []
    for employee in employees:
        entries = entries_by_employee.get(employee.id, [])
        last = latest.get(employee.id)
        result.append({
            "employee": employee,
            "task": current_task(employee, tasks_by_employee.get(employee.id, [])),
            "open_tasks": len(tasks_by_employee.get(employee.id, [])),
            "hours_today": hours_worked(employee, entries=entries, end=day_end),
            "clocked_in": last is not None and last.entry_type == IN,
            "last_seen": last.timestamp if last else None,
            "warehouse": employee.warehouse,
        })
    return result


def on_shift_count(warehouse=None) -> int:
    return sum(1 for row in roster(warehouse) if row["clocked_in"])
