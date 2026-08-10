"""Colours, fonts and formatting helpers, taken from the design PDF."""
from decimal import Decimal, InvalidOperation

# --- Palette ------------------------------------------------------------------------
TEAL = "#005F6B"          # navigation bar
TEAL_DARK = "#00454E"     # active navigation tab
TEAL_HOVER = "#00747F"
ACCENT = "#00A896"        # reminder markers, positive accents

BG = "#E9EBEC"            # application background
CARD = "#FFFFFF"
HEADER_BG = "#FFFFFF"

TEXT = "#1A1D1F"
TEXT_MUTED = "#6B7280"
TEXT_INVERSE = "#FFFFFF"

BORDER = "#DFE3E6"
BORDER_STRONG = "#1A1D1F"
ROW_LINE = "#EFF1F2"
HOVER = "#F4F6F7"
SELECTED = "#DCEEF0"

DANGER = "#D32F2F"
DANGER_HOVER = "#B71C1C"
WARNING = "#F5A623"
SUCCESS = "#00A15A"
INFO = "#004AAD"

# Metric card colours from the Analytics mock-up
CARD_NET_SALES = "#004AAD"
CARD_PROFIT = "#00C853"
CARD_TRANSACTIONS = "#6200EA"
CARD_ITEMS_SOLD = "#E05A52"
CARD_COSTS = "#4A4A4A"

# Categorical series colours for charts, in draw order
CHART_SERIES = ["#00A896", "#E4685D", "#F5A623", "#F4B8B8", "#6200EA", "#004AAD"]

FONT_FAMILY = "Segoe UI"


# --- Formatting ---------------------------------------------------------------------

def money(value, symbol="₹", decimals=2) -> str:
    """Format using the Indian digit grouping: ₹5,00,000.00"""
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return f"{symbol}0.00"

    negative = amount < 0
    amount = abs(amount)
    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    amount = amount.quantize(quant)

    whole, _, frac = f"{amount:f}".partition(".")
    grouped = _indian_grouping(whole)
    text = f"{symbol}{grouped}"
    if decimals:
        text += "." + (frac or "0" * decimals).ljust(decimals, "0")[:decimals]
    return f"-{text}" if negative else text


def _indian_grouping(digits: str) -> str:
    """Last three digits, then pairs: 5000000 -> 50,00,000"""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def quantity(value, decimals=3) -> str:
    """Trim pointless zeros: 5.000 -> 5, 2.500 -> 2.5"""
    try:
        d = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return "0"
    d = d.quantize(Decimal(1).scaleb(-decimals)).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return f"{d:f}"


def percent(value) -> str:
    try:
        return f"{Decimal(str(value or 0)).normalize():f}%"
    except (InvalidOperation, ValueError):
        return "0%"


def date_short(value) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def datetime_short(value) -> str:
    return value.strftime("%d %b %Y, %H:%M") if value else "—"
