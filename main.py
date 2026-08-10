"""Inventory Management System — application entry point."""
import logging
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

import config
from ui import theme


def setup_logging():
    handlers = [logging.FileHandler(config.LOG_PATH, encoding="utf-8")]

    # A windowed PyInstaller build has no console: sys.stdout can be None, and a
    # StreamHandler wrapped around it would raise on the first log call.
    if getattr(sys, "stdout", None) is not None and hasattr(sys.stdout, "write"):
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("inventory")


def install_exception_hook(log):
    """Log unhandled exceptions and tell the user, instead of vanishing silently."""
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception",
                     exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Something went wrong")
        box.setText("The application hit an unexpected error.")
        box.setInformativeText(
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"The full details were written to:\n{config.LOG_PATH}"
        )
        box.setDetailedText(detail)
        box.exec()
    sys.excepthook = hook


def load_stylesheet(app, log):
    try:
        app.setStyleSheet(config.STYLES_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("Could not load stylesheet %s: %s", config.STYLES_PATH, exc)


def start_database(log):
    """Connect and create the schema. Returns True on success."""
    from database.connection import DatabaseNotConfigured
    from services import bootstrap

    try:
        bootstrap.initialize()
        log.info("Database ready (backend=%s)", config.DB_BACKEND)
        return True
    except DatabaseNotConfigured as exc:
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Database not configured")
        box.setText("The application could not open its database.")
        box.setInformativeText(
            f"{exc}\n\n"
            f"Edit the .env file beside the application:\n"
            f"  • DB_BACKEND=sqlite  — work locally, no server needed\n"
            f"  • DB_BACKEND=postgres — plus DB_HOST, DB_NAME, DB_USER, DB_PASSWORD"
        )
        box.exec()
        log.error("Database not configured: %s", exc)
        return False
    except Exception as exc:
        log.exception("Database startup failed")
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Database error")
        box.setText("The database could not be prepared.")
        box.setInformativeText(str(exc))
        box.setDetailedText(traceback.format_exc())
        box.exec()
        return False


def main():
    log = setup_logging()
    log.info("Starting Inventory Management System")

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Inventory Management System")
    app.setOrganizationName("Inventory")
    app.setFont(QFont(theme.FONT_FAMILY, 10))

    install_exception_hook(log)
    load_stylesheet(app, log)

    if not start_database(log):
        return 1

    from ui.login import sign_in
    from ui.main_window import MainWindow

    while True:
        user = sign_in()
        if user is None:
            log.info("Sign-in cancelled; exiting")
            return 0

        log.info("Signed in as %s (%s)", user.username, user.role)
        window = MainWindow(user)
        window.signed_out = False
        window.show()
        app.exec()

        if not getattr(window, "signed_out", False):
            log.info("Window closed; exiting")
            return 0
        log.info("User signed out; returning to the sign-in screen")


if __name__ == "__main__":
    sys.exit(main())
