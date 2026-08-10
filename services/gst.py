"""GST helpers: Indian state codes, GSTIN validation, and the CGST/SGST vs IGST split."""
import re
from decimal import Decimal

ZERO = Decimal("0")

# GST state code -> state name. The code is the first two digits of a GSTIN.
STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh", "97": "Other Territory",
}

STATE_CHOICES = sorted(
    ((code, f"{name} ({code})") for code, name in STATE_CODES.items()),
    key=lambda pair: pair[1],
)

GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$")

COMMON_GST_RATES = [Decimal("0"), Decimal("5"), Decimal("12"), Decimal("18"),
                    Decimal("28")]


def state_name(code):
    return STATE_CODES.get((code or "").strip(), "")


def is_valid_gstin(gstin) -> bool:
    """Structural check plus the state-code lookup. Not a government verification."""
    gstin = (gstin or "").strip().upper()
    if len(gstin) != 15 or not GSTIN_PATTERN.match(gstin):
        return False
    return gstin[:2] in STATE_CODES


def gstin_problem(gstin, allow_blank=True):
    """Human-readable reason a GSTIN is unacceptable, or None."""
    gstin = (gstin or "").strip()
    if not gstin:
        return None if allow_blank else "GSTIN is required."
    if len(gstin) != 15:
        return f"A GSTIN is 15 characters; this one has {len(gstin)}."
    if not is_valid_gstin(gstin):
        return "That GSTIN is not in a valid format (e.g. 27AAACS1234A1Z5)."
    return None


def state_code_from_gstin(gstin):
    gstin = (gstin or "").strip().upper()
    return gstin[:2] if len(gstin) >= 2 and gstin[:2] in STATE_CODES else None


def is_interstate(supply_state_code, party_state_code) -> bool:
    """Interstate when the two GST states differ.

    If either side's state is unknown we assume intrastate — the safer default, since it
    keeps the tax split visible on the invoice rather than silently charging IGST.
    """
    a = (supply_state_code or "").strip()
    b = (party_state_code or "").strip()
    if not a or not b:
        return False
    return a != b


def split_tax(taxable_amount, gst_rate, interstate: bool):
    """Return (cgst, sgst, igst) for one taxable amount.

    Intrastate splits the rate in half across CGST and SGST; interstate charges the full
    rate as IGST.
    """
    amount = Decimal(str(taxable_amount or 0))
    rate = Decimal(str(gst_rate or 0))
    total_tax = (amount * rate / Decimal("100")).quantize(Decimal("0.01"))

    if interstate:
        return ZERO, ZERO, total_tax

    half = (total_tax / Decimal("2")).quantize(Decimal("0.01"))
    # Give any rounding remainder to CGST so the halves always sum to the total.
    return total_tax - half, half, ZERO


def round_off(total):
    """Round an invoice total to the nearest rupee; returns (rounded, adjustment)."""
    amount = Decimal(str(total or 0))
    rounded = amount.quantize(Decimal("1"))
    return rounded, rounded - amount
