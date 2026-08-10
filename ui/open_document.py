"""Open a generated PDF in whatever the machine uses for PDFs."""
import logging
import os
import subprocess
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from ui.widgets import common

log = logging.getLogger(__name__)


def open_document(parent, path, label=None):
    """Show the finished document, falling back to telling the user where it is."""
    if not path or not os.path.exists(path):
        common.error(parent, "Document missing",
                     "The file was not written. Check the log for details.")
        return False

    opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    if not opened:
        # Qt can decline on a machine with no PDF handler registered.
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606 - opening the file we just wrote
                opened = True
            else:
                subprocess.Popen(["xdg-open", str(path)])
                opened = True
        except Exception:
            log.exception("Could not open %s", path)

    if not opened:
        common.info(parent, label or "Document ready",
                    "The PDF was created but this PC has no application registered "
                    "to open it.", f"Saved to:\n{path}")
    return True


def reveal_folder(parent, path):
    """Open the containing folder, for someone who wants to print several at once."""
    folder = os.path.dirname(str(path))
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
