"""A form dialog driven by a field specification.

The master-data records (warehouses, suppliers, customers, categories, employees, users)
are all "a handful of fields plus validation", so they share this one dialog rather than
five near-identical hand-written forms.
"""
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from typing import Any, Callable, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from services import gst
from ui import theme
from ui.widgets import common


@dataclass
class Field:
    """One form field. `kind` decides which widget is built."""
    key: str
    label: str
    kind: str = "text"          # text password textarea int decimal money percent
                                # check combo state date static
    required: bool = False
    placeholder: str = ""
    default: Any = None
    choices: Optional[Callable] = None       # () -> records or (value, label) pairs
    label_fn: Callable = str
    allow_blank: bool = True
    blank_text: str = "— none —"
    decimals: int = 2
    maximum: float = 99_999_999.0
    max_length: Optional[int] = None
    help: Optional[str] = None
    validator: Optional[Callable] = None     # value -> error text or None
    uppercase: bool = False
    section: Optional[str] = None
    enabled: bool = True


@dataclass
class FormSpec:
    title: str
    fields: list
    width: int = 620
    height: int = 620
    intro: Optional[str] = None
    extras: dict = dataclass_field(default_factory=dict)


class RecordDialog(QDialog):
    """Builds a form from `spec`, validates it, and hands the values to `save_fn`.

    `save_fn(values: dict, instance) -> record` is responsible for persistence; any
    exception it raises is shown inline rather than crashing the dialog.
    """

    def __init__(self, parent, spec: FormSpec, instance=None, save_fn=None,
                 read_only=False):
        super().__init__(parent)
        self.spec = spec
        self.instance = instance
        self.save_fn = save_fn
        self.read_only = read_only
        self.saved_record = None
        self.widgets = {}

        verb = "Edit" if instance is not None else "New"
        self.setWindowTitle(f"{verb} {spec.title}")
        self.setMinimumSize(spec.width, min(spec.height, 720))
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel(f"{verb} {spec.title}")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        if spec.intro:
            layout.addWidget(common.subtle(spec.intro))

        layout.addWidget(self._build_form(), 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button(
            "Close" if read_only else "Cancel", self.reject, "action"))
        if not read_only:
            buttons.addWidget(common.action_button("Save", self.submit, "primary"))
        layout.addLayout(buttons)

        self._populate()

    # --- Build ----------------------------------------------------------------------

    def _build_form(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(2, 6, 2, 6)
        outer.setSpacing(10)

        form = None
        current_section = object()

        for field in self.spec.fields:
            if field.section != current_section:
                current_section = field.section
                if field.section:
                    outer.addWidget(common.Divider())
                    outer.addWidget(common.section_title(field.section))
                form = QFormLayout()
                form.setVerticalSpacing(11)
                form.setLabelAlignment(Qt.AlignLeft)
                outer.addLayout(form)

            widget = self._build_widget(field)
            self.widgets[field.key] = widget
            widget.setEnabled(field.enabled and not self.read_only)

            label = common.field_label(field.label + (" *" if field.required else ""))
            if field.kind == "check":
                form.addRow("", widget)
            else:
                form.addRow(label, widget)

            if field.help:
                note = common.subtle(field.help)
                note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
                form.addRow("", note)

        outer.addStretch()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(page)
        return area

    def _build_widget(self, field: Field):
        kind = field.kind

        if kind in ("text", "password"):
            widget = QLineEdit()
            widget.setPlaceholderText(field.placeholder)
            if field.max_length:
                widget.setMaxLength(field.max_length)
            if kind == "password":
                widget.setEchoMode(QLineEdit.Password)
            return widget

        if kind == "textarea":
            widget = QPlainTextEdit()
            widget.setPlaceholderText(field.placeholder)
            widget.setFixedHeight(72)
            return widget

        if kind == "int":
            widget = QSpinBox()
            widget.setRange(0, int(field.maximum))
            widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            widget.setButtonSymbols(QSpinBox.NoButtons)
            return widget

        if kind in ("decimal", "money", "percent"):
            prefix = "₹ " if kind == "money" else None
            widget = common.DecimalSpin(decimals=field.decimals,
                                        maximum=field.maximum, prefix=prefix)
            if kind == "percent":
                widget.setSuffix(" %")
                widget.setMaximum(100.0)
            return widget

        if kind == "check":
            return common.checkbox(field.label)

        if kind == "date":
            widget = QDateEdit()
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("dd MMM yyyy")
            widget.setDate(QDate.currentDate())
            return widget

        if kind == "state":
            widget = common.ComboField(allow_blank=True, blank_text="— select state —")
            widget.load_choices(gst.STATE_CHOICES)
            return widget

        if kind == "combo":
            widget = common.ComboField(allow_blank=field.allow_blank,
                                       blank_text=field.blank_text)
            values = field.choices() if field.choices else []
            if values and isinstance(values[0], tuple):
                widget.load_choices(values)
            else:
                widget.load(values, label=field.label_fn)
            return widget

        if kind == "static":
            widget = QLabel("—")
            widget.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            return widget

        raise ValueError(f"Unknown field kind '{kind}' for '{field.key}'")

    # --- Values ---------------------------------------------------------------------

    def _populate(self):
        for field in self.spec.fields:
            widget = self.widgets[field.key]
            value = (getattr(self.instance, field.key, None)
                     if self.instance is not None else field.default)
            if value is None and self.instance is None:
                value = field.default
            self._set_value(field, widget, value)

    def _set_value(self, field, widget, value):
        kind = field.kind
        if kind in ("text", "password"):
            widget.setText("" if value is None else str(value))
        elif kind == "textarea":
            widget.setPlainText("" if value is None else str(value))
        elif kind == "int":
            widget.setValue(int(value or 0))
        elif kind in ("decimal", "money", "percent"):
            widget.setValue(float(value or 0))
        elif kind == "check":
            widget.setChecked(bool(value) if value is not None else bool(field.default))
        elif kind == "date":
            if value:
                widget.setDate(QDate(value.year, value.month, value.day))
        elif kind == "state":
            if value:
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
        elif kind == "combo":
            if value is not None:
                if not widget.select_record(value):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
        elif kind == "static":
            widget.setText("—" if value is None else str(value))

    def _get_value(self, field):
        widget = self.widgets[field.key]
        kind = field.kind
        if kind in ("text", "password"):
            text = widget.text().strip()
            return (text.upper() if field.uppercase else text) or None
        if kind == "textarea":
            return widget.toPlainText().strip() or None
        if kind == "int":
            return widget.value()
        if kind in ("decimal", "money", "percent"):
            return Decimal(str(round(widget.value(), field.decimals)))
        if kind == "check":
            return widget.isChecked()
        if kind == "date":
            return widget.date().toPython()
        if kind in ("state", "combo"):
            return widget.current()
        return None

    def values(self):
        return {f.key: self._get_value(f) for f in self.spec.fields
                if f.kind != "static"}

    # --- Save -----------------------------------------------------------------------

    def _fail(self, message, key=None):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        if key and key in self.widgets:
            self.widgets[key].setFocus()
        return False

    def validate(self):
        for field in self.spec.fields:
            if field.kind == "static":
                continue
            value = self._get_value(field)
            if field.required and (value is None or value == "" or
                                   (field.kind in ("decimal", "money") and value == 0
                                    and field.default not in (None, 0))):
                return self._fail(f"{field.label} is required.", field.key)
            if field.validator is not None:
                problem = field.validator(value)
                if problem:
                    return self._fail(problem, field.key)
        return True

    def submit(self):
        self.error_label.setVisible(False)
        if not self.validate():
            return
        try:
            self.saved_record = self.save_fn(self.values(), self.instance)
            self.accept()
        except Exception as exc:
            self._fail(str(exc))


# --- Shared validators --------------------------------------------------------------

def gstin_validator(value):
    return gst.gstin_problem(value, allow_blank=True)


def make_unique_validator(model, model_field, instance=None, label="value"):
    """Reject a duplicate on a unique column, ignoring the record being edited."""
    def check(value):
        if value in (None, ""):
            return None
        existing = model.get_or_none(model_field == value)
        if existing is None:
            return None
        if instance is not None and existing.id == instance.id:
            return None
        name = getattr(existing, "name", None) or getattr(existing, "full_name", str(existing))
        return f"That {label} is already used by {name}."
    return check


def email_validator(value):
    if not value:
        return None
    if "@" not in value or "." not in value.split("@")[-1]:
        return "Enter a valid email address."
    return None


def pincode_validator(value):
    if not value:
        return None
    digits = str(value).strip()
    if not digits.isdigit() or len(digits) != 6:
        return "An Indian PIN code is 6 digits."
    return None
