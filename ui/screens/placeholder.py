"""Placeholder for screens scheduled in a later phase.

It renders the real toolbar and column headings from the design so the layout is
reviewable, but says plainly that the screen is not wired up yet rather than pretending
to work.
"""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui import theme
from ui.widgets import common
from ui.widgets.table import Column, DataTable


class PlaceholderScreen(QWidget):
    def __init__(self, title, columns, phase, summary, capabilities=None,
                 toolbar_buttons=None, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.addWidget(common.screen_title(title.upper()))
        toolbar.addStretch()
        for label in (toolbar_buttons or []):
            button = common.action_button(label, None, "action")
            button.setEnabled(False)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        banner = common.Card(padding=16)
        heading = QLabel(f"Scheduled for {phase}")
        heading.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEAL};"
        )
        banner.add(heading)
        banner.add(common.subtle(summary))
        if capabilities:
            bullets = "".join(f"<li style='margin-bottom:4px;'>{c}</li>"
                              for c in capabilities)
            detail = QLabel(f"<ul style='margin-left:-18px;'>{bullets}</ul>")
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 13px;")
            banner.add(detail)
        layout.addWidget(banner)

        table = DataTable(
            [Column(name, width="stretch") for name in columns],
            checkable=False,
            empty_text="This screen is not connected to the database yet.",
        )
        table.setEnabled(False)
        layout.addWidget(table, 1)
