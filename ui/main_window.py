"""Main application window: header, teal navigation bar and the screen stack."""
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

import logging
import time

import config
from database.models import Role
from services import alerts, auth
from services import search as search_service
from ui.dialogs.search_dialog import SearchResultsDialog

log = logging.getLogger(__name__)
from ui import theme
from ui.screens.analytics_screen import AnalyticsScreen
from ui.screens.home_screen import HomeScreen
from ui.screens.logs_screen import LogsScreen
from ui.screens.operations import InventoryArea
from ui.screens.receiving_screen import ReceivingScreen
from ui.screens.settings_screen import SettingsScreen
from ui.screens.shipping_screen import ShippingScreen
from ui.widgets import common

# Screen indices in the stack
HOME, SHIPPING, RECEIVING, INVENTORY, LOGS, ANALYTICS, SETTINGS = range(7)


class HomeButton(QPushButton):
    """The house icon at the left of the navigation bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("NavHome")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Home")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        rect = self.rect()
        cx, cy = rect.center().x() + 1, rect.center().y() + 2
        w, h = 20, 15
        # Walls
        painter.drawLine(cx - w // 2, cy - 2, cx - w // 2, cy + h // 2)
        painter.drawLine(cx + w // 2, cy - 2, cx + w // 2, cy + h // 2)
        painter.drawLine(cx - w // 2, cy + h // 2, cx + w // 2, cy + h // 2)
        # Roof
        painter.drawLine(cx - w // 2 - 2, cy - 1, cx, cy - h // 2 - 3)
        painter.drawLine(cx + w // 2 + 2, cy - 1, cx, cy - h // 2 - 3)
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self, user):
        super().__init__()
        self.user = user
        self.setWindowTitle("Inventory Management System")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_navbar())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        # Alerts first: the Home screen reads reminders as it builds, so generating
        # them afterwards would leave it showing a stale count until something else
        # triggered a refresh.
        self._refresh_alerts()
        self._build_screens()
        self._build_statusbar()
        self._build_shortcuts()
        self._start_idle_lock()

        self.go_to(INVENTORY)

    # --- Idle lock ------------------------------------------------------------------

    def _start_idle_lock(self):
        """Lock the session after a spell of inactivity.

        Warehouse terminals are shared and get walked away from. Without this, whoever
        wanders past inherits the last person's permissions.
        """
        self.signed_out = False
        self._locking = False
        self._last_activity = time.monotonic()
        self._idle_seconds = max(0, config.SESSION_IDLE_MINUTES) * 60
        if not self._idle_seconds:
            return

        QApplication.instance().installEventFilter(self)
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(15_000)
        self._idle_timer.timeout.connect(self._check_idle)
        self._idle_timer.start()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.MouseMove, QEvent.MouseButtonPress,
                            QEvent.KeyPress, QEvent.Wheel, QEvent.TouchBegin):
            self._last_activity = time.monotonic()
        return super().eventFilter(obj, event)

    def _check_idle(self):
        if self._locking or not self._idle_seconds:
            return
        if time.monotonic() - self._last_activity < self._idle_seconds:
            return
        self._lock()

    def _lock(self):
        from ui.login import LockDialog

        self._locking = True
        try:
            auth.record_audit(self.user, "LOCK",
                              summary=f"{self.user.full_name}'s session locked on idle")
            dialog = LockDialog(self, user=self.user,
                                minutes=config.SESSION_IDLE_MINUTES)
            if dialog.exec() and dialog.unlocked:
                self._last_activity = time.monotonic()
                self.refresh_current()
                return
            # Cancelled or failed: end the session rather than leave it open.
            self.signed_out = True
            self.close()
        finally:
            self._locking = False

    def _refresh_alerts(self):
        """Regenerate the automatic reminders once per sign-in.

        Deliberately never fatal: a failure here must not stop someone working.
        """
        try:
            summary = alerts.refresh_all()
            if summary["created"] or summary["resolved"]:
                log.info("Alerts refreshed: %s new, %s resolved",
                         summary["created"], summary["resolved"])
        except Exception:
            log.exception("Could not refresh automatic reminders")

    # --- Header ---------------------------------------------------------------------

    def _build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(14)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        logo_row.addWidget(common.LogoWidget(size=40, colour=theme.TEAL))
        brand = QLabel("Inventory")
        brand.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {theme.TEAL};"
        )
        logo_row.addWidget(brand)
        layout.addLayout(logo_row)

        layout.addSpacing(10)
        self.search = common.SearchBox()
        self.search.setMaximumWidth(560)
        self.search.setMinimumWidth(320)
        self.search.search_submitted.connect(self._on_search_submitted)
        self.search.text_changed.connect(self._on_search_typed)
        layout.addWidget(self.search, 1)
        layout.addStretch()

        user_box = QVBoxLayout()
        user_box.setSpacing(0)
        name = QLabel(self.user.full_name)
        name.setObjectName("HeaderUserName")
        name.setAlignment(Qt.AlignRight)
        role = QLabel(Role.LABELS.get(self.user.role, self.user.role)
                      + (f" · {self.user.warehouse.name}" if self.user.warehouse else ""))
        role.setObjectName("HeaderUserRole")
        role.setAlignment(Qt.AlignRight)
        user_box.addWidget(name)
        user_box.addWidget(role)
        layout.addLayout(user_box)

        self.settings_button = common.IconButton("gear", 38)
        self.settings_button.setToolTip("Settings and master data")
        self.settings_button.clicked.connect(lambda: self.go_to(SETTINGS))
        layout.addWidget(self.settings_button)

        return header

    # --- Navigation -----------------------------------------------------------------

    def _build_navbar(self):
        bar = QFrame()
        bar.setObjectName("NavBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(0)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}

        home = HomeButton()
        home.clicked.connect(lambda: self.go_to(HOME))
        self.nav_group.addButton(home, HOME)
        self.nav_buttons[HOME] = home
        layout.addWidget(home)

        entries = [
            (SHIPPING, "Shipping", None),
            (RECEIVING, "Receiving", None),
            (INVENTORY, "Inventory", auth.PERM_VIEW_INVENTORY),
            (LOGS, "Recent Logs", None),
            (ANALYTICS, "Analytics", auth.PERM_VIEW_ANALYTICS),
        ]
        for index, label, permission in entries:
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, i=index: self.go_to(i))
            if permission and not auth.can(self.user, permission):
                button.setVisible(False)
            self.nav_group.addButton(button, index)
            self.nav_buttons[index] = button
            layout.addWidget(button)

        layout.addStretch()

        self.lock_button = QPushButton("Sign out")
        self.lock_button.setObjectName("NavButton")
        self.lock_button.setCursor(Qt.PointingHandCursor)
        self.lock_button.clicked.connect(self.sign_out)
        layout.addWidget(self.lock_button)

        return bar

    def _build_screens(self):
        self.inventory_screen = InventoryArea(self.user)
        self.settings_screen = SettingsScreen(self.user)
        self.shipping_screen = ShippingScreen(self.user)
        self.receiving_screen = ReceivingScreen(self.user)
        self.logs_screen = LogsScreen(self.user)
        self.analytics_screen = AnalyticsScreen(self.user)
        self.home_screen = HomeScreen(self.user)

        for screen in (self.home_screen, self.shipping_screen, self.receiving_screen,
                       self.inventory_screen, self.logs_screen, self.analytics_screen,
                       self.settings_screen):
            self.stack.addWidget(screen)

    def _build_statusbar(self):
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        backend = "SQLite (local)" if config.DB_BACKEND == "sqlite" else \
                  f"PostgreSQL · {config.PG_HOST}"
        self.status_message = QLabel(
            f"Signed in as {self.user.username}  ·  {backend}"
        )
        self.status_message.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 12px; padding: 2px 8px;"
        )
        bar.addWidget(self.status_message)
        self.setStatusBar(bar)

    def _build_shortcuts(self):
        find = QAction(self)
        find.setShortcut(QKeySequence.Find)
        find.triggered.connect(lambda: self.search.input.setFocus())
        self.addAction(find)

        refresh = QAction(self)
        refresh.setShortcut(QKeySequence.Refresh)
        refresh.triggered.connect(self.refresh_current)
        self.addAction(refresh)

    # --- Navigation behaviour -------------------------------------------------------

    def go_to(self, index):
        self.stack.setCurrentIndex(index)
        button = self.nav_buttons.get(index)
        if button is not None:
            button.setChecked(True)
        else:
            # Settings has no nav button; clear the current highlight.
            checked = self.nav_group.checkedButton()
            if checked is not None:
                self.nav_group.setExclusive(False)
                checked.setChecked(False)
                self.nav_group.setExclusive(True)
        self.refresh_current()

    def refresh_current(self):
        screen = self.stack.currentWidget()
        if hasattr(screen, "refresh"):
            screen.refresh()

    # --- Search ---------------------------------------------------------------------

    def _on_search_typed(self, text):
        """Live-filter whichever list screen is showing."""
        screen = self.stack.currentWidget()
        if hasattr(screen, "set_search_text"):
            screen.set_search_text(text)

    def _on_search_submitted(self, text):
        """Enter: a scanned barcode opens that item; anything else lists what matched."""
        if not text:
            return

        scanned = search_service.scanned_item(text)
        if scanned is not None:
            self.go_to(INVENTORY)
            self.inventory_screen.set_search_text(scanned.sku)
            self.inventory_screen._edit_item(scanned)
            return

        results = search_service.search(text)
        if not results:
            self.status_message.setText(f"Nothing matches '{text}'.")
            return

        if len(results) == 1:
            self.open_search_result(results[0])
            return

        dialog = SearchResultsDialog(self, term=text, results=results)
        if dialog.exec() and dialog.chosen is not None:
            self.open_search_result(dialog.chosen)

    def open_search_result(self, result):
        """Take the user to the screen that record lives on, filtered to it."""
        kind = result.kind
        record = result.payload

        if kind == search_service.ITEM:
            self.go_to(INVENTORY)
            self.inventory_screen.set_search_text(record.sku)
        elif kind == search_service.SALES_ORDER:
            self.go_to(SHIPPING)
            self._show_all_statuses(self.shipping_screen)
            self.shipping_screen.set_search_text(record.number)
        elif kind == search_service.PURCHASE_ORDER:
            self.go_to(RECEIVING)
            self._show_all_statuses(self.receiving_screen)
            self.receiving_screen.set_search_text(record.number)
        elif kind == search_service.CUSTOMER:
            self.go_to(SHIPPING)
            self._show_all_statuses(self.shipping_screen)
            self.shipping_screen.customer_combo.select_record(record)
            self.shipping_screen.refresh()
        elif kind == search_service.SUPPLIER:
            self.go_to(RECEIVING)
            self._show_all_statuses(self.receiving_screen)
            self.receiving_screen.supplier_combo.select_record(record)
            self.receiving_screen.refresh()
        elif kind == search_service.EMPLOYEE:
            self.go_to(HOME)
            self.home_screen.refresh()
            self.status_message.setText(
                f"{record.name} — badge {record.code}"
                + (f" · {record.designation}" if record.designation else ""))

    @staticmethod
    def _show_all_statuses(screen):
        """A searched-for order might be closed, so drop the 'open only' default."""
        combo = getattr(screen, "status_combo", None)
        if combo is None:
            return
        index = combo.findData("__all__")
        if index >= 0:
            combo.setCurrentIndex(index)

    # --- Session --------------------------------------------------------------------

    def sign_out(self):
        if not common.confirm(self, "Sign out?",
                              f"Sign out of {self.user.full_name}'s session?",
                              confirm_label="Sign out"):
            return
        auth.record_audit(self.user, "LOGOUT", summary=f"{self.user.full_name} signed out")
        self.signed_out = True
        self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
