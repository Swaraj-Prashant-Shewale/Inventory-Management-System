"""Login window, plus the first-run wizard shown when the database has no users yet."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from services import auth, bootstrap, gst
from ui import theme
from ui.widgets import common


class LoginDialog(QDialog):
    """Collects credentials. On success `self.user` holds the authenticated User."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.user = None
        self.setWindowTitle("Sign in — Inventory Management System")
        self.setObjectName("LoginBackdrop")
        self.setFixedSize(880, 520)
        self.setModal(True)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_branding(), 4)
        outer.addWidget(self._build_form(), 5)

        self.username_input.setFocus()

    # --- Left panel -----------------------------------------------------------------

    def _build_branding(self):
        panel = QWidget()
        panel.setObjectName("LoginBrandPanel")
        # Qualify by object name: an unscoped rule here would cascade onto every child
        # widget and repaint the buttons too.
        panel.setStyleSheet(
            f"QWidget#LoginBrandPanel {{ background-color: {theme.TEAL}; }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(44, 44, 44, 44)
        layout.setSpacing(16)
        layout.addStretch()

        logo = common.LogoWidget(size=84, colour="#FFFFFF")
        layout.addWidget(logo, 0, Qt.AlignLeft)

        title = QLabel("Inventory\nManagement")
        title.setStyleSheet("color: white; font-size: 30px; font-weight: 700;")
        layout.addWidget(title)

        blurb = QLabel(
            "Multi-warehouse stock, purchasing, shipping and analytics — "
            "in one place."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color: #BFE0E4; font-size: 14px;")
        layout.addWidget(blurb)
        layout.addStretch()
        return panel

    # --- Right panel ----------------------------------------------------------------

    def _build_form(self):
        panel = QWidget()
        panel.setObjectName("LoginFormPanel")
        panel.setStyleSheet("QWidget#LoginFormPanel { background-color: white; }")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(50, 50, 50, 50)
        layout.setSpacing(14)
        layout.addStretch()

        heading = QLabel("Sign in")
        heading.setObjectName("LoginTitle")
        layout.addWidget(heading)

        sub = QLabel("Enter your credentials to continue.")
        sub.setObjectName("LoginSubtitle")
        layout.addWidget(sub)
        layout.addSpacing(14)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setMinimumHeight(42)
        self.username_input.returnPressed.connect(self._focus_password)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMinimumHeight(42)
        self.password_input.returnPressed.connect(self.attempt_login)

        show_password = common.checkbox("Show password")
        show_password.toggled.connect(
            lambda on: self.password_input.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )

        layout.addWidget(common.field_label("Username"))
        layout.addWidget(self.username_input)
        layout.addWidget(common.field_label("Password"))
        layout.addWidget(self.password_input)
        layout.addWidget(show_password)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        layout.addSpacing(6)
        self.login_button = common.action_button("Sign in", self.attempt_login, "primary")
        self.login_button.setMinimumHeight(44)
        self.login_button.setDefault(True)
        layout.addWidget(self.login_button)

        layout.addStretch()
        return panel

    def _focus_password(self):
        self.password_input.setFocus()

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        for widget in (self.username_input, self.password_input):
            widget.setProperty("invalid", "true")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _clear_error(self):
        self.error_label.setVisible(False)
        for widget in (self.username_input, self.password_input):
            widget.setProperty("invalid", "false")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def attempt_login(self):
        self._clear_error()
        self.login_button.setEnabled(False)
        try:
            self.user = auth.authenticate(
                self.username_input.text(), self.password_input.text()
            )
            self.accept()
        except auth.AuthError as exc:
            self._show_error(str(exc))
            self.password_input.clear()
            self.password_input.setFocus()
        except Exception as exc:  # database trouble, etc.
            self._show_error(f"Could not sign in: {exc}")
        finally:
            self.login_button.setEnabled(True)


class FirstRunDialog(QDialog):
    """Creates the owner account and the first warehouse on a brand-new database."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.user = None
        self.setWindowTitle("Set up your Inventory Management System")
        self.setMinimumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        heading = QLabel("Welcome — let's create your owner account")
        heading.setObjectName("LoginTitle")
        layout.addWidget(heading)

        layout.addWidget(common.subtle(
            "This database is empty. The account you create here is the Owner: the only "
            "role that can manage users, company details and delete records. "
            "You can add managers and clerks afterwards."
        ))
        layout.addWidget(common.Divider())

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignLeft)

        self.company_input = QLineEdit()
        self.company_input.setPlaceholderText("Your registered legal name")

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Your full name")

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Used to sign in")

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("At least 8 characters")

        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)

        self.warehouse_name = QLineEdit("Main Warehouse")
        self.warehouse_code = QLineEdit("WH1")

        self.state_combo = common.ComboField(allow_blank=True,
                                             blank_text="— select state —")
        self.state_combo.load_choices(gst.STATE_CHOICES)

        form.addRow(common.field_label("Company name"), self.company_input)
        form.addRow(common.field_label("Your name"), self.name_input)
        form.addRow(common.field_label("Username"), self.username_input)
        form.addRow(common.field_label("Password"), self.password_input)
        form.addRow(common.field_label("Confirm password"), self.confirm_input)
        form.addRow(common.field_label("First warehouse"), self.warehouse_name)
        form.addRow(common.field_label("Warehouse code"), self.warehouse_code)
        form.addRow(common.field_label("Warehouse state"), self.state_combo)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Create account", self.submit, "primary"))
        layout.addLayout(buttons)

    def _fail(self, message, widget=None):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        if widget:
            widget.setFocus()
        return False

    def submit(self):
        self.error_label.setVisible(False)

        name = self.name_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not name:
            return self._fail("Enter your name.", self.name_input)
        if not username:
            return self._fail("Choose a username.", self.username_input)
        if " " in username:
            return self._fail("Usernames cannot contain spaces.", self.username_input)
        if password != self.confirm_input.text():
            return self._fail("The two passwords do not match.", self.confirm_input)
        problem = auth.password_problem(password)
        if problem:
            return self._fail(problem, self.password_input)
        if not self.warehouse_name.text().strip():
            return self._fail("Give your first warehouse a name.", self.warehouse_name)
        if not self.warehouse_code.text().strip():
            return self._fail("Give your first warehouse a short code.",
                              self.warehouse_code)

        try:
            self.user = bootstrap.create_first_owner(
                username=username,
                password=password,
                full_name=name,
                warehouse_name=self.warehouse_name.text().strip(),
                warehouse_code=self.warehouse_code.text().strip().upper(),
                state_code=self.state_combo.current(),
            )
            company = self.company_input.text().strip()
            if company:
                from database.models import CompanySettings
                settings = CompanySettings.get_or_none()
                if settings:
                    settings.legal_name = company
                    settings.state_code = self.state_combo.current()
                    settings.state = gst.state_name(self.state_combo.current())
                    settings.save()
            self.accept()
        except Exception as exc:
            return self._fail(str(exc))


class ChangePasswordDialog(QDialog):
    """Force a new password. Used after a reset, and available from the lock screen."""

    def __init__(self, parent=None, user=None, require_current=True, reason=None):
        super().__init__(parent)
        self.user = user
        self.require_current = require_current
        self.changed = False

        self.setWindowTitle("Change password")
        self.setMinimumWidth(460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Choose a new password")
        heading.setObjectName("LoginTitle")
        layout.addWidget(heading)
        layout.addWidget(common.subtle(
            reason or "Your password was reset by an owner, so it must be changed "
                      "before you can continue."))

        form = QFormLayout()
        form.setVerticalSpacing(12)

        self.current_input = QLineEdit()
        self.current_input.setEchoMode(QLineEdit.Password)
        if require_current:
            form.addRow(common.field_label("Current password"), self.current_input)

        self.new_input = QLineEdit()
        self.new_input.setEchoMode(QLineEdit.Password)
        self.new_input.setPlaceholderText("At least 8 characters")
        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.returnPressed.connect(self.submit)
        form.addRow(common.field_label("New password"), self.new_input)
        form.addRow(common.field_label("Confirm"), self.confirm_input)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Change password", self.submit,
                                               "primary"))
        layout.addLayout(buttons)

    def _fail(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def submit(self):
        self.error_label.setVisible(False)
        if self.new_input.text() != self.confirm_input.text():
            return self._fail("The two passwords do not match.")
        try:
            auth.change_password(
                self.user, self.new_input.text(),
                current_password=self.current_input.text(),
                require_current=self.require_current,
            )
            self.changed = True
            self.accept()
        except auth.AuthError as exc:
            self._fail(str(exc))


class LockDialog(QDialog):
    """Idle lock. The signed-in user re-enters their password, or the session ends."""

    def __init__(self, parent=None, user=None, minutes=0):
        super().__init__(parent)
        self.user = user
        self.unlocked = False

        self.setWindowTitle("Session locked")
        self.setMinimumWidth(460)
        self.setModal(True)
        # No close button: the only ways out are the password or Sign out.
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        logo_row = QHBoxLayout()
        logo_row.addStretch()
        logo_row.addWidget(common.LogoWidget(size=52, colour=theme.TEAL))
        logo_row.addStretch()
        layout.addLayout(logo_row)

        heading = QLabel("Session locked")
        heading.setObjectName("LoginTitle")
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading)

        note = QLabel(f"Locked after {minutes} minutes of inactivity.<br>"
                      f"Signed in as <b>{user.full_name}</b>.")
        note.setObjectName("LoginSubtitle")
        note.setAlignment(Qt.AlignCenter)
        note.setTextFormat(Qt.RichText)
        layout.addWidget(note)
        layout.addSpacing(6)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("Password")
        self.password_input.setMinimumHeight(42)
        self.password_input.returnPressed.connect(self.submit)
        layout.addWidget(self.password_input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addWidget(common.action_button("Sign out", self.reject, "action"))
        buttons.addStretch()
        buttons.addWidget(common.action_button("Unlock", self.submit, "primary"))
        layout.addLayout(buttons)

        self.password_input.setFocus()

    def submit(self):
        self.error_label.setVisible(False)
        try:
            auth.authenticate(self.user.username, self.password_input.text())
            self.unlocked = True
            auth.record_audit(self.user, "UNLOCK",
                              summary=f"{self.user.full_name} unlocked their session")
            self.accept()
        except auth.AuthError as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            self.password_input.clear()
            self.password_input.setFocus()


def sign_in(parent=None):
    """Run first-run setup if needed, then the login dialog. Returns a User or None."""
    if bootstrap.needs_setup():
        setup = FirstRunDialog(parent)
        if setup.exec() != QDialog.Accepted:
            return None
        common.info(
            parent, "Account created",
            f"Owner account '{setup.user.username}' is ready.",
            "Sign in with it to start using the system.",
        )

    dialog = LoginDialog(parent)
    if dialog.exec() != QDialog.Accepted:
        return None

    user = dialog.user
    # A reset password is temporary by definition; make it actually temporary.
    while user.must_change_password:
        change = ChangePasswordDialog(parent, user=user, require_current=True)
        if change.exec() != QDialog.Accepted:
            common.warn(parent, "Password not changed",
                        "You must set a new password before signing in.")
            return None
        user = type(user).get_by_id(user.id)
    return user
