"""Reusable widgets and small helpers shared by every screen."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme


# --- Containers ---------------------------------------------------------------------

class Card(QFrame):
    """White rounded panel — the building block of every screen in the design."""

    def __init__(self, parent=None, padding=18, spacing=12, shadow=True):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)
        if shadow:
            apply_shadow(self)

    def layout(self):
        return self._layout

    def add(self, widget, stretch=0):
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch=0):
        self._layout.addLayout(layout, stretch)
        return layout


def apply_shadow(widget, blur=18, alpha=26, y_offset=2):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setColor(QColor(0, 0, 0, alpha))
    effect.setOffset(0, y_offset)
    widget.setGraphicsEffect(effect)
    return effect


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


def vspace(height=1):
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if height <= 1
                    else QSizePolicy.Fixed)
    if height > 1:
        w.setFixedHeight(height)
    return w


# --- Text ---------------------------------------------------------------------------

def screen_title(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("ScreenTitle")
    return label


def section_title(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def subtle(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Subtle")
    label.setWordWrap(True)
    return label


def field_label(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("FieldLabel")
    return label


class StatusPill(QLabel):
    """Small coloured chip used for order and stock statuses."""

    PALETTE = {
        "neutral": ("#EFF1F2", "#4A5157"),
        "info": ("#DCEAF7", "#1B4F8A"),
        "success": ("#D8F3E5", "#0A6B41"),
        "warning": ("#FDEDD3", "#8A5A00"),
        "danger": ("#FBE0E0", "#A32020"),
        "teal": ("#D5EDF0", "#00454E"),
    }

    def __init__(self, text="", tone="neutral", parent=None):
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.setAlignment(Qt.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone):
        bg, fg = self.PALETTE.get(tone, self.PALETTE["neutral"])
        self.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border-radius: 10px;"
            f" padding: 3px 12px; font-size: 12px; font-weight: 700;"
        )


# --- Buttons ------------------------------------------------------------------------

def action_button(text, on_click=None, kind="action", tooltip=None, enabled=True):
    """kind: 'action' (outlined), 'primary' (teal), 'danger' (red), 'ghost' (link)."""
    button = QPushButton(text)
    button.setObjectName({
        "action": "Action", "primary": "Primary", "danger": "Danger", "ghost": "Ghost",
    }.get(kind, "Action"))
    button.setCursor(Qt.PointingHandCursor)
    button.setEnabled(enabled)
    if tooltip:
        button.setToolTip(tooltip)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


class IconButton(QPushButton):
    """Round flat button that paints a simple glyph — used for the settings gear."""

    def __init__(self, glyph="gear", size=36, colour=theme.TEXT, parent=None):
        super().__init__(parent)
        self.setObjectName("IconButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(size, size)
        self._glyph = glyph
        self._colour = QColor(colour)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._colour, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        rect = self.rect()
        cx, cy = rect.center().x() + 1, rect.center().y() + 1
        if self._glyph == "gear":
            radius = min(rect.width(), rect.height()) * 0.24
            painter.drawEllipse(int(cx - radius), int(cy - radius),
                                int(radius * 2), int(radius * 2))
            painter.save()
            painter.translate(cx, cy)
            for _ in range(8):
                painter.drawLine(0, int(-radius - 4), 0, int(-radius - 1))
                painter.rotate(45)
            painter.restore()
        elif self._glyph == "refresh":
            radius = int(min(rect.width(), rect.height()) * 0.28)
            painter.drawArc(int(cx - radius), int(cy - radius),
                            radius * 2, radius * 2, 40 * 16, 280 * 16)
            painter.drawLine(int(cx + radius * 0.75), int(cy - radius * 0.95),
                             int(cx + radius * 1.15), int(cy - radius * 0.25))
            painter.drawLine(int(cx + radius * 0.75), int(cy - radius * 0.95),
                             int(cx + radius * 0.05), int(cy - radius * 0.95))
        painter.end()


class LogoWidget(QWidget):
    """The stacked-parcel mark from the top-left of the design."""

    def __init__(self, size=38, colour=theme.TEAL, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._colour = QColor(colour)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._colour, 1.8)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        w = self.width()
        unit = w / 10.0
        # Box front face
        left, top, right, bottom = unit * 1.4, unit * 3.4, unit * 7.4, unit * 8.6
        painter.drawRect(int(left), int(top), int(right - left), int(bottom - top))
        # Lid, drawn as a simple isometric top
        painter.drawLine(int(left), int(top), int(left + unit * 1.4), int(top - unit * 1.6))
        painter.drawLine(int(right), int(top), int(right + unit * 1.4), int(top - unit * 1.6))
        painter.drawLine(int(left + unit * 1.4), int(top - unit * 1.6),
                         int(right + unit * 1.4), int(top - unit * 1.6))
        painter.drawLine(int(right), int(bottom), int(right + unit * 1.4),
                         int(bottom - unit * 1.6))
        painter.drawLine(int(right + unit * 1.4), int(bottom - unit * 1.6),
                         int(right + unit * 1.4), int(top - unit * 1.6))
        # Tape down the middle
        painter.drawLine(int((left + right) / 2), int(top), int((left + right) / 2), int(bottom))
        painter.end()


# --- Inputs -------------------------------------------------------------------------

class MagnifierIcon(QWidget):
    """Painted magnifier — a drawn shape beats a unicode glyph that varies by font."""

    def __init__(self, size=18, colour=theme.TEXT, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._colour = QColor(colour)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._colour, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        d = self.width()
        radius = d * 0.32
        cx = cy = d * 0.42
        painter.drawEllipse(int(cx - radius), int(cy - radius),
                            int(radius * 2), int(radius * 2))
        painter.drawLine(int(cx + radius * 0.72), int(cy + radius * 0.72),
                         int(d - 2), int(d - 2))
        painter.end()


class SearchBox(QFrame):
    """Bordered search field with a clear button, as drawn in the header."""

    search_submitted = Signal(str)
    text_changed = Signal(str)

    def __init__(self, placeholder="Search items, orders, suppliers, customers…",
                 parent=None):
        super().__init__(parent)
        self.setObjectName("SearchBox")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        layout.setSpacing(6)

        icon = MagnifierIcon(18)

        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.setClearButtonEnabled(False)
        self.input.returnPressed.connect(self._submit)
        self.input.textChanged.connect(self.text_changed.emit)
        self.input.textChanged.connect(self._toggle_clear)

        self.clear_button = QPushButton("✕")
        self.clear_button.setObjectName("SearchClear")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setFixedWidth(26)
        self.clear_button.clicked.connect(self.clear)
        self.clear_button.setVisible(False)

        layout.addWidget(icon)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.clear_button)

    def _toggle_clear(self, text):
        self.clear_button.setVisible(bool(text))

    def _submit(self):
        self.search_submitted.emit(self.input.text().strip())

    def text(self):
        return self.input.text().strip()

    def clear(self):
        self.input.clear()
        self.input.setFocus()

    def set_placeholder(self, text):
        self.input.setPlaceholderText(text)


class DecimalSpin(QDoubleSpinBox):
    """Quantity/price input with sane defaults and no spin arrows crowding the field."""

    def __init__(self, decimals=2, maximum=99_999_999.0, prefix=None, parent=None):
        super().__init__(parent)
        self.setDecimals(decimals)
        self.setRange(0.0, maximum)
        self.setGroupSeparatorShown(True)
        self.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if prefix:
            self.setPrefix(prefix)

    def decimal_value(self):
        from decimal import Decimal
        return Decimal(str(round(self.value(), self.decimals())))


class ComboField(QComboBox):
    """Combo that carries model objects as item data."""

    def __init__(self, parent=None, allow_blank=False, blank_text="— none —"):
        super().__init__(parent)
        self._allow_blank = allow_blank
        self._blank_text = blank_text

    def load(self, records, label=str, selected=None):
        self.clear()
        if self._allow_blank:
            self.addItem(self._blank_text, None)
        for record in records:
            self.addItem(label(record), record)
        if selected is not None:
            self.select_record(selected)
        return self

    def load_choices(self, pairs, selected=None):
        """pairs: iterable of (value, label)."""
        self.clear()
        if self._allow_blank:
            self.addItem(self._blank_text, None)
        for value, label in pairs:
            self.addItem(label, value)
        if selected is not None:
            index = self.findData(selected)
            if index >= 0:
                self.setCurrentIndex(index)
        return self

    def select_record(self, record):
        target = getattr(record, "id", record)
        for i in range(self.count()):
            data = self.itemData(i)
            if data is None:
                continue
            if getattr(data, "id", data) == target:
                self.setCurrentIndex(i)
                return True
        return False

    def current(self):
        return self.currentData()


def checkbox(text, checked=False) -> QCheckBox:
    box = QCheckBox(text)
    box.setChecked(checked)
    box.setCursor(Qt.PointingHandCursor)
    return box


# --- Dialog helpers -----------------------------------------------------------------

def _box(parent, icon, title, text, informative=None):
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    return box


def info(parent, title, text, informative=None):
    _box(parent, QMessageBox.Information, title, text, informative).exec()


def warn(parent, title, text, informative=None):
    _box(parent, QMessageBox.Warning, title, text, informative).exec()


def error(parent, title, text, informative=None):
    _box(parent, QMessageBox.Critical, title, text, informative).exec()


def choose_from_menu(parent, anchor, choices):
    """Drop a small menu under `anchor` and return the chosen key, or None.

    A plain function rather than an inline QMenu so it can be substituted in tests —
    QMenu.exec is a C++ method that cannot be monkeypatched, and it blocks forever
    without someone to click it.
    """
    from PySide6.QtWidgets import QMenu

    if not choices:
        return None
    if len(choices) == 1:
        return choices[0][0]

    menu = QMenu(parent)
    actions = {menu.addAction(label): key for key, label in choices}
    chosen = menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
    return actions.get(chosen)


def confirm(parent, title, text, informative=None, confirm_label="Confirm",
            destructive=False) -> bool:
    box = _box(parent, QMessageBox.Warning if destructive else QMessageBox.Question,
               title, text, informative)
    yes = box.addButton(confirm_label, QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(yes)
    if destructive:
        yes.setStyleSheet(
            f"background-color: {theme.DANGER}; color: white; border-radius: 7px;"
            f" padding: 7px 20px; font-weight: 600;"
        )
    box.exec()
    return box.clickedButton() is yes
