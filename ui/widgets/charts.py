"""Chart widgets built on QtCharts, to one fixed set of marks.

The palette is validated, not chosen by eye: the six colour checks (lightness band,
chroma floor, CVD separation, normal-vision separation, contrast) pass for these hues on
a white card surface. The design PDF's original chart colours did not — its light pink
read as grey and sat below the normal-vision separation floor against its orange.

Mark specs, applied everywhere: 2px lines with round caps, markers at least 8px with a
2px surface ring, hairline solid gridlines one step off the surface, a legend whenever
there are two or more series and none when there is one, and direct labels used
sparingly rather than a number on every point.
"""
from decimal import Decimal

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSet,
    QChart,
    QChartView,
    QHorizontalBarSeries,
    QLegend,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import QMargins, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QToolTip, QVBoxLayout, QWidget

from ui import theme

# --- Validated categorical slots (light surface #FFFFFF) ----------------------------
# Assigned in fixed order and never cycled; a series keeps its colour when others are
# filtered out, so identity never depends on rank.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]

GRID = "#E1E0D9"          # hairline, one step off the surface
AXIS_LINE = "#C3C2B7"
AXIS_TEXT = "#898781"
SURFACE = "#FFFFFF"

MAX_SERIES = len(SERIES)


def series_colour(index: int) -> str:
    """Fixed-order assignment. Past the last slot the caller must fold into 'Other'."""
    if index >= MAX_SERIES:
        raise ValueError(
            f"Only {MAX_SERIES} validated categorical colours exist; fold the tail "
            f"into 'Other' or facet rather than generating a new hue."
        )
    return SERIES[index]


def _f(value) -> float:
    if value is None:
        return 0.0
    return float(value) if not isinstance(value, Decimal) else float(value)


def _style_chart(chart: QChart, legend: bool):
    chart.setBackgroundVisible(False)
    chart.setPlotAreaBackgroundVisible(False)
    chart.setMargins(QMargins(0, 4, 0, 0))
    chart.setBackgroundRoundness(0)
    chart.setAnimationOptions(QChart.NoAnimation)
    if legend:
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignTop)
        chart.legend().setMarkerShape(QLegend.MarkerShapeCircle)
        chart.legend().setFont(QFont(theme.FONT_FAMILY, 9))
        # Legend text wears a text token, never the series colour.
        chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))
        chart.legend().setBackgroundVisible(False)
    else:
        chart.legend().setVisible(False)


def _style_axis(axis, label_angle=0):
    axis.setLabelsColor(QColor(AXIS_TEXT))
    axis.setLabelsFont(QFont(theme.FONT_FAMILY, 9))
    axis.setGridLineColor(QColor(GRID))
    axis.setGridLinePen(QPen(QColor(GRID), 1, Qt.SolidLine))   # solid, never dashed
    axis.setLinePenColor(QColor(AXIS_LINE))
    axis.setLabelsAngle(label_angle)
    axis.setTitleVisible(False)
    if isinstance(axis, QValueAxis):
        axis.setMinorTickCount(0)


def _nice_ceiling(value: float) -> float:
    """Round the axis maximum up to a clean number so ticks read 0 / 10 / 20."""
    if value <= 0:
        return 10.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        candidate = magnitude * step
        if candidate >= value:
            return candidate
    return magnitude * 10


class ChartCard(QWidget):
    """A titled chart with an optional period selector, sized to include its axis band."""

    def __init__(self, title, chart_view, control=None, parent=None, height=260):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        header.addWidget(label)
        header.addStretch()
        if control is not None:
            header.addWidget(control)
        layout.addLayout(header)

        # Height covers the plot plus the x-axis band, so labels are never cut off.
        chart_view.setMinimumHeight(height)
        chart_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(chart_view, 1)


class HoverChartView(QChartView):
    """Chart view that shows a tooltip on hover — values are never tooltip-only."""

    def __init__(self, chart, parent=None):
        super().__init__(chart, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMouseTracking(True)

    def show_point(self, point: QPointF, state: bool, text):
        if state:
            QToolTip.showText(self.mapToGlobal(
                self.chart().mapToPosition(point).toPoint()), text, self)
        else:
            QToolTip.hideText()


def line_chart(axis_labels, series_specs, value_formatter=None, y_title=None):
    """Multi-series line chart.

    `series_specs`: [{"name": str, "values": [numbers or None]}]. A legend appears
    only when there are two or more series; the last point of each series is
    direct-labelled so identity never rests on colour alone.
    """
    if len(series_specs) > MAX_SERIES:
        raise ValueError("Too many series for the validated palette — fold or facet.")

    chart = QChart()
    _style_chart(chart, legend=len(series_specs) > 1)

    formatter = value_formatter or (lambda v: f"{v:,.0f}")
    maximum = 0.0

    x_axis = QBarCategoryAxis()
    x_axis.append([label for _, label in axis_labels])
    _style_axis(x_axis)
    chart.addAxis(x_axis, Qt.AlignBottom)

    y_axis = QValueAxis()
    _style_axis(y_axis)
    chart.addAxis(y_axis, Qt.AlignLeft)

    view = HoverChartView(chart)

    for index, spec in enumerate(series_specs):
        colour = QColor(series_colour(index))
        line = QLineSeries()
        line.setName(spec["name"])
        pen = QPen(colour, 2)                     # 2px, round join/cap
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        line.setPen(pen)

        points = []
        for position, value in enumerate(spec["values"]):
            if value is None:
                continue
            numeric = _f(value)
            maximum = max(maximum, numeric)
            line.append(position, numeric)
            points.append((position, numeric))

        chart.addSeries(line)
        line.attachAxis(x_axis)
        line.attachAxis(y_axis)

        # End marker: >= 8px, filled in the series colour, 2px ring in the surface.
        if points:
            marker = QScatterSeries()
            marker.setMarkerSize(9)
            marker.setColor(colour)
            marker.setBorderColor(QColor(SURFACE))
            marker.setPen(QPen(QColor(SURFACE), 2))
            marker.append(points[-1][0], points[-1][1])
            chart.addSeries(marker)
            marker.attachAxis(x_axis)
            marker.attachAxis(y_axis)
            # The end dot restates the line; keep it out of the legend.
            for legend_marker in chart.legend().markers(marker):
                legend_marker.setVisible(False)

        name = spec["name"]
        line.hovered.connect(
            lambda point, state, v=view, n=name, f=formatter:
            v.show_point(point, state, f"{n}: {f(point.y())}")
        )

    y_axis.setRange(0, _nice_ceiling(maximum))
    y_axis.setTickCount(5)
    y_axis.setLabelFormat("%d")
    return view


def horizontal_bar_chart(entries, value_formatter=None):
    """Ranked horizontal bars — one hue, because a single series needs no identity.

    Used where the design PDF drew a donut: comparing four close values in a donut is
    unreliable, and the ranking reads directly off bar length here.
    `entries`: [{"label": str, "value": number}], already sorted.
    """
    formatter = value_formatter or (lambda v: f"{v:,.0f}")

    chart = QChart()
    _style_chart(chart, legend=False)          # single series: the title names it

    bar_set = QBarSet("")
    bar_set.setColor(QColor(SERIES[0]))
    bar_set.setBorderColor(QColor(SERIES[0]))
    bar_set.setLabelColor(QColor(theme.TEXT))
    bar_set.setLabelFont(QFont(theme.FONT_FAMILY, 9, QFont.DemiBold))

    # Chart draws bottom-up, so reverse to put the largest at the top.
    ordered = list(reversed(entries))
    maximum = 0.0
    for entry in ordered:
        value = _f(entry["value"])
        maximum = max(maximum, value)
        bar_set.append(value)

    series = QHorizontalBarSeries()
    series.append(bar_set)
    series.setBarWidth(0.55)                   # leave air in the band; cap thickness
    series.setLabelsVisible(True)              # value at the tip
    series.setLabelsPosition(QHorizontalBarSeries.LabelsOutsideEnd)
    series.setLabelsFormat("@value")
    chart.addSeries(series)

    y_axis = QBarCategoryAxis()
    y_axis.append([e["label"] for e in ordered])
    _style_axis(y_axis)
    y_axis.setGridLineVisible(False)
    chart.addAxis(y_axis, Qt.AlignLeft)
    series.attachAxis(y_axis)

    x_axis = QValueAxis()
    _style_axis(x_axis)
    x_axis.setRange(0, _nice_ceiling(maximum * 1.18))   # headroom for the tip label
    x_axis.setTickCount(4)
    x_axis.setLabelFormat("%d")
    chart.addAxis(x_axis, Qt.AlignBottom)
    series.attachAxis(x_axis)

    view = HoverChartView(chart)
    series.hovered.connect(
        lambda status, index, _s=None, v=view, o=ordered, f=formatter:
        QToolTip.showText(v.mapToGlobal(v.rect().center()),
                          f"{o[index]['label']}: {f(_f(o[index]['value']))}", v)
        if status and 0 <= index < len(o) else QToolTip.hideText()
    )
    return view


class StatTile(QWidget):
    """Headline figure: label, value, optional delta against a named period."""

    def __init__(self, label, value, colour, subtitle=None, parent=None):
        super().__init__(parent)
        self.setObjectName("StatTile")
        self.setFixedHeight(104)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # A bare QWidget subclass ignores a stylesheet background without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#StatTile {{ background-color: {colour}; border-radius: 14px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(2)

        self.value_label = QLabel(str(value))
        # Proportional figures: tabular-nums makes a large number look loose.
        self.value_label.setStyleSheet(
            "color: white; font-size: 26px; font-weight: 700; background: transparent;"
        )
        self.value_label.setAlignment(Qt.AlignCenter)

        self.caption = QLabel(label)
        self.caption.setStyleSheet(
            "color: rgba(255,255,255,0.88); font-size: 12px; font-weight: 600;"
            " background: transparent;"
        )
        self.caption.setAlignment(Qt.AlignCenter)

        layout.addStretch()
        layout.addWidget(self.value_label)
        layout.addWidget(self.caption)
        if subtitle:
            note = QLabel(subtitle)
            note.setStyleSheet(
                "color: rgba(255,255,255,0.75); font-size: 11px; background: transparent;"
            )
            note.setAlignment(Qt.AlignCenter)
            layout.addWidget(note)
        layout.addStretch()

    def set_value(self, value, subtitle=None):
        self.value_label.setText(str(value))


class EmptyChart(QWidget):
    """Shown instead of an empty plot, so a blank card never looks like a bug."""

    def __init__(self, message="No data for this period yet.", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(message)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 13px; background: transparent;"
        )
        layout.addWidget(label)
