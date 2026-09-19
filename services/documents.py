"""Printable documents: GST tax invoice, delivery challan, purchase order, GRN, labels.

Everything is generated from the record plus CompanySettings, so a document can never
show figures that disagree with the database. The tax invoice follows the layout Indian
GST requires: both parties' GSTIN and state, HSN per line, the CGST/SGST or IGST split,
the total in words, and a rounded grand total.
"""
import datetime
import logging
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config
from database.models import CompanySettings, PaymentStatus, ZERO
from services import gst

log = logging.getLogger(__name__)

TEAL = colors.HexColor("#005F6B")
INK = colors.HexColor("#1A1D1F")
MUTED = colors.HexColor("#6B7280")
LINE = colors.HexColor("#C9CFD3")
BAND = colors.HexColor("#EFF2F3")


class DocumentError(Exception):
    """A document could not be produced. Safe to show the user."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


# --- Shared helpers -----------------------------------------------------------------

def output_dir():
    config.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    return config.DOCUMENTS_DIR


def safe_name(text) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "document")).strip("-")


def company():
    settings = CompanySettings.get_or_none()
    if settings is None:
        raise DocumentError(
            "Company details have not been set. Open Settings → Company first.")
    return settings


def company_gaps(settings=None):
    """What is still missing before a tax invoice would be legally valid."""
    settings = settings or company()
    missing = []
    if not settings.legal_name or settings.legal_name == "My Company":
        missing.append("legal name")
    if not settings.gstin:
        missing.append("GSTIN")
    if not settings.address_line1:
        missing.append("registered address")
    if not settings.state_code:
        missing.append("state")
    return missing


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty",
         "Ninety"]


def _two_digits(number: int) -> str:
    if number < 20:
        return _ONES[number]
    return (_TENS[number // 10] + (" " + _ONES[number % 10] if number % 10 else "")).strip()


def _three_digits(number: int) -> str:
    parts = []
    if number >= 100:
        parts.append(_ONES[number // 100] + " Hundred")
        number %= 100
    if number:
        parts.append(_two_digits(number))
    return " ".join(parts)


def amount_in_words(amount) -> str:
    """Indian numbering: crore, lakh, thousand. Required on a GST invoice."""
    amount = _dec(amount).quantize(Decimal("0.01"))
    negative = amount < 0
    amount = abs(amount)
    rupees = int(amount)
    paise = int((amount - rupees) * 100)

    if rupees == 0:
        words = "Zero"
    else:
        groups = []
        for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"),
                               (1_000, "Thousand")):
            if rupees >= divisor:
                count = rupees // divisor
                rupees %= divisor
                groups.append(f"{_three_digits(count)} {label}")
        if rupees:
            groups.append(_three_digits(rupees))
        words = " ".join(groups)

    text = f"Rupees {words}"
    if paise:
        text += f" and {_two_digits(paise)} Paise"
    text += " Only"
    return ("Minus " + text) if negative else text


def money(value) -> str:
    """Plain digits for table cells; the ₹ sign lives in the column heading."""
    amount = _dec(value).quantize(Decimal("0.01"))
    negative = amount < 0
    whole, _, frac = f"{abs(amount):f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if negative else "") + f"{whole}.{(frac or '00')[:2].ljust(2, '0')}"


def quantity(value) -> str:
    number = _dec(value).normalize()
    if number == number.to_integral_value():
        number = number.quantize(Decimal(1))
    return f"{number:f}"


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=15, textColor=INK, spaceAfter=2,
                                alignment=TA_CENTER),
        "company": ParagraphStyle("c", parent=base["Normal"],
                                  fontName="Helvetica-Bold", fontSize=13,
                                  textColor=TEAL, leading=16),
        "body": ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica",
                               fontSize=8.5, textColor=INK, leading=11.5),
        "small": ParagraphStyle("s", parent=base["Normal"], fontName="Helvetica",
                                fontSize=7.5, textColor=MUTED, leading=10),
        "label": ParagraphStyle("l", parent=base["Normal"], fontName="Helvetica-Bold",
                                fontSize=8, textColor=MUTED, leading=11),
        "right": ParagraphStyle("r", parent=base["Normal"], fontName="Helvetica",
                                fontSize=8.5, textColor=INK, alignment=TA_RIGHT,
                                leading=11.5),
        "cell": ParagraphStyle("ce", parent=base["Normal"], fontName="Helvetica",
                               fontSize=8, textColor=INK, leading=10.5),
        "warn": ParagraphStyle("w", parent=base["Normal"], fontName="Helvetica-Bold",
                               fontSize=8, textColor=colors.HexColor("#B3261E"),
                               leading=11),
    }


def _address_block(name, gstin=None, state_code=None, lines=(), phone=None, email=None):
    parts = [f"<b>{name}</b>"]
    parts += [line for line in lines if line]
    if gstin:
        parts.append(f"GSTIN: {gstin}")
    if state_code:
        parts.append(f"State: {gst.state_name(state_code)} ({state_code})")
    if phone:
        parts.append(f"Phone: {phone}")
    if email:
        parts.append(email)
    return "<br/>".join(parts)


def _header(story, styles, settings, title, subtitle=None):
    left = [Paragraph(settings.legal_name or "—", styles["company"])]
    if settings.trade_name:
        left.append(Paragraph(settings.trade_name, styles["small"]))
    detail = [line for line in (settings.address_line1, settings.address_line2,
                                ", ".join(filter(None, [settings.city,
                                                        settings.pincode]))) if line]
    if settings.state_code:
        detail.append(f"{gst.state_name(settings.state_code)} ({settings.state_code})")
    if settings.gstin:
        detail.append(f"GSTIN: {settings.gstin}")
    contact = ", ".join(filter(None, [settings.phone, settings.email]))
    if contact:
        detail.append(contact)
    left.append(Paragraph("<br/>".join(detail), styles["body"]))

    right = [Paragraph(title, styles["title"])]
    if subtitle:
        right.append(Paragraph(subtitle, styles["small"]))

    table = Table([[left, right]], colWidths=[110 * mm, 65 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 1, TEAL),
    ]))
    story.append(table)
    story.append(Spacer(1, 5 * mm))


def _meta_row(story, styles, pairs, columns=4):
    cells = []
    for label, value in pairs:
        cells.append(Paragraph(f"{label}<br/><font size=9 color='#1A1D1F'>"
                               f"{value or '—'}</font>", styles["label"]))
    while len(cells) % columns:
        cells.append("")
    rows = [cells[i:i + columns] for i in range(0, len(cells), columns)]
    width = 175 * mm / columns
    table = Table(rows, colWidths=[width] * columns)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 4 * mm))


def _line_table(header, rows, widths, aligns):
    table = Table([header] + rows, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFA")]),
    ]
    for column, align in enumerate(aligns):
        style.append(("ALIGN", (column, 0), (column, -1), align))
    table.setStyle(TableStyle(style))
    return table


def _totals(story, styles, pairs, emphasis_last=True):
    rows = [[Paragraph(label, styles["right"]), Paragraph(value, styles["right"])]
            for label, value in pairs]
    table = Table(rows, colWidths=[135 * mm, 40 * mm], hAlign="RIGHT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, LINE),
    ]
    if emphasis_last:
        style += [
            ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), BAND),
            ("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1), 0.8, TEAL),
        ]
    table.setStyle(TableStyle(style))
    story.append(table)


def _signature(story, styles, settings, caption="Authorised Signatory"):
    story.append(Spacer(1, 12 * mm))
    block = Table([[
        Paragraph("", styles["small"]),
        Paragraph(f"For <b>{settings.legal_name or '—'}</b><br/><br/><br/>{caption}",
                  styles["right"]),
    ]], colWidths=[110 * mm, 65 * mm])
    block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(block)


def _build(path, story, title):
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=title, author="Inventory Management System",
    )

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(17 * mm, 9 * mm,
                          f"Generated {datetime.datetime.now():%d %b %Y %H:%M}")
        canvas.drawRightString(A4[0] - 17 * mm, 9 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return str(path)


# --- Tax invoice --------------------------------------------------------------------

def tax_invoice(invoice, path=None) -> str:
    """GST tax invoice for a sales order."""
    settings = company()
    order = invoice.order
    customer = invoice.customer
    styles = _styles()
    path = path or output_dir() / f"{safe_name(invoice.number)}.pdf"

    story = []
    _header(story, styles, settings, "TAX INVOICE",
            "Original for Recipient" if True else None)

    gaps = company_gaps(settings)
    if gaps:
        story.append(Paragraph(
            f"NOT A VALID TAX INVOICE — missing {', '.join(gaps)}. "
            f"Complete Settings → Company and reissue.", styles["warn"]))
        story.append(Spacer(1, 3 * mm))

    billing = _address_block(
        customer.name, customer.gstin, customer.state_code,
        [customer.billing_address], customer.phone, customer.email)
    shipping = _address_block(
        customer.name, None, None,
        [order.delivery_address or customer.delivery_address,
         f"PIN {order.delivery_pincode}" if order.delivery_pincode else None])

    parties = Table([[
        Paragraph("BILL TO", styles["label"]),
        Paragraph("SHIP TO", styles["label"]),
    ], [
        Paragraph(billing, styles["body"]),
        Paragraph(shipping, styles["body"]),
    ]], colWidths=[87.5 * mm, 87.5 * mm])
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ]))
    story.append(parties)
    story.append(Spacer(1, 4 * mm))

    _meta_row(story, styles, [
        ("INVOICE NO.", invoice.number),
        ("DATE", invoice.invoice_date.strftime("%d %b %Y")),
        ("ORDER REF.", order.number),
        ("DUE DATE", invoice.due_date.strftime("%d %b %Y") if invoice.due_date else "—"),
        ("PLACE OF SUPPLY", invoice.place_of_supply or "—"),
        ("SUPPLY TYPE", "Inter-State (IGST)" if invoice.is_interstate
         else "Intra-State (CGST+SGST)"),
        ("TRUCK / VEHICLE", order.truck_hsrp or "—"),
        ("PAYMENT", PaymentStatus.LABELS.get(order.payment_status,
                                             order.payment_status)),
    ])

    interstate = invoice.is_interstate
    if interstate:
        header = ["#", "Description", "HSN", "Qty", "Rate", "Taxable",
                  "IGST %", "IGST", "Total"]
        widths = [8, 52, 15, 14, 19, 21, 14, 18, 22]
        aligns = ["CENTER", "LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT", "CENTER",
                  "RIGHT", "RIGHT"]
    else:
        header = ["#", "Description", "HSN", "Qty", "Rate", "Taxable",
                  "CGST", "SGST", "Total"]
        widths = [8, 50, 15, 14, 19, 21, 18, 18, 20]
        aligns = ["CENTER", "LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT", "RIGHT",
                  "RIGHT", "RIGHT"]

    rows = []
    hsn_summary = {}
    for index, line in enumerate(order.lines, start=1):
        taxable = _dec(line.line_total)
        cgst, sgst, igst = gst.split_tax(taxable, line.gst_rate, interstate)
        total = taxable + cgst + sgst + igst
        hsn = line.hsn_code or "—"

        bucket = hsn_summary.setdefault(
            (hsn, _dec(line.gst_rate)), {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO,
                                         "igst": ZERO})
        bucket["taxable"] += taxable
        bucket["cgst"] += cgst
        bucket["sgst"] += sgst
        bucket["igst"] += igst

        cells = [str(index), Paragraph(line.item.name, styles["cell"]), hsn,
                 quantity(line.quantity), money(line.unit_price), money(taxable)]
        if interstate:
            cells += [f"{_dec(line.gst_rate):g}%", money(igst)]
        else:
            cells += [money(cgst), money(sgst)]
        cells.append(money(total))
        rows.append(cells)

    story.append(_line_table(header, rows, [w * mm for w in widths], aligns))
    story.append(Spacer(1, 4 * mm))

    totals = [("Taxable Value", money(invoice.subtotal))]
    if interstate:
        totals.append(("IGST", money(invoice.igst)))
    else:
        totals += [("CGST", money(invoice.cgst)), ("SGST", money(invoice.sgst))]
    if _dec(invoice.round_off) != 0:
        totals.append(("Round Off", money(invoice.round_off)))
    totals.append(("Grand Total (INR)", money(invoice.total)))
    _totals(story, styles, totals)

    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"<b>Amount in words:</b> {amount_in_words(invoice.total)}",
                           styles["body"]))

    if len(hsn_summary) > 1:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("HSN SUMMARY", styles["label"]))
        summary_header = (["HSN", "Rate", "Taxable", "IGST", "Total"] if interstate
                          else ["HSN", "Rate", "Taxable", "CGST", "SGST", "Total"])
        summary_rows = []
        for (hsn, rate), bucket in sorted(hsn_summary.items()):
            row = [hsn, f"{rate:g}%", money(bucket["taxable"])]
            if interstate:
                row.append(money(bucket["igst"]))
            else:
                row += [money(bucket["cgst"]), money(bucket["sgst"])]
            row.append(money(bucket["taxable"] + bucket["cgst"] + bucket["sgst"]
                             + bucket["igst"]))
            summary_rows.append(row)
        column_count = len(summary_header)
        story.append(_line_table(
            summary_header, summary_rows,
            [175 * mm / column_count] * column_count,
            ["CENTER"] + ["RIGHT"] * (column_count - 1)))

    bank = [line for line in (
        f"Bank: {settings.bank_name}" if settings.bank_name else None,
        f"A/C: {settings.bank_account}" if settings.bank_account else None,
        f"IFSC: {settings.bank_ifsc}" if settings.bank_ifsc else None) if line]
    if bank or settings.invoice_terms:
        story.append(Spacer(1, 5 * mm))
        notes = []
        if bank:
            notes.append("<b>Bank Details</b><br/>" + "<br/>".join(bank))
        if settings.invoice_terms:
            notes.append("<b>Terms</b><br/>" + settings.invoice_terms.replace("\n", "<br/>"))
        story.append(Paragraph("<br/><br/>".join(notes), styles["small"]))

    _signature(story, styles, settings)
    return _build(path, story, f"Tax Invoice {invoice.number}")


# --- Delivery challan ---------------------------------------------------------------

def delivery_challan(fulfillment, path=None) -> str:
    """The document that travels with the goods. Quantities, no prices."""
    settings = company()
    order = fulfillment.order
    customer = order.customer
    styles = _styles()
    path = path or output_dir() / f"{safe_name(fulfillment.number)}-challan.pdf"

    story = []
    _header(story, styles, settings, "DELIVERY CHALLAN",
            "Not a tax invoice · goods description only")

    consignee = _address_block(
        customer.name, customer.gstin, customer.state_code,
        [order.delivery_address or customer.delivery_address,
         f"PIN {order.delivery_pincode}" if order.delivery_pincode else None],
        customer.phone)
    parties = Table([[Paragraph("CONSIGNEE", styles["label"]),
                      Paragraph("DESPATCHED FROM", styles["label"])],
                     [Paragraph(consignee, styles["body"]),
                      Paragraph(_address_block(
                          fulfillment.warehouse.name, None,
                          fulfillment.warehouse.state_code,
                          [fulfillment.warehouse.address_line1,
                           fulfillment.warehouse.city]), styles["body"])]],
                    colWidths=[87.5 * mm, 87.5 * mm])
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ]))
    story.append(parties)
    story.append(Spacer(1, 4 * mm))

    _meta_row(story, styles, [
        ("CHALLAN NO.", fulfillment.number),
        ("DATE", fulfillment.ship_date.strftime("%d %b %Y")),
        ("ORDER REF.", order.number),
        ("TRUCK / VEHICLE", fulfillment.truck_hsrp or "—"),
    ])

    rows = []
    total_units = ZERO
    for index, line in enumerate(fulfillment.lines, start=1):
        total_units += _dec(line.quantity)
        rows.append([
            str(index),
            Paragraph(line.item.name, styles["cell"]),
            line.item.sku,
            line.lot.lot_number if line.lot else "—",
            line.item.base_uom.code if line.item.base_uom else "—",
            quantity(line.quantity),
        ])
    story.append(_line_table(
        ["#", "Description", "SKU", "Batch", "UOM", "Quantity"],
        rows,
        [10 * mm, 70 * mm, 30 * mm, 27 * mm, 16 * mm, 22 * mm],
        ["CENTER", "LEFT", "LEFT", "CENTER", "CENTER", "RIGHT"]))

    story.append(Spacer(1, 3 * mm))
    _totals(story, styles, [("Total units", quantity(total_units))],
            emphasis_last=False)

    story.append(Spacer(1, 10 * mm))
    receipt = Table([[
        Paragraph("Received the goods described above in good order and condition."
                  "<br/><br/><br/>Receiver's signature, name and date",
                  styles["small"]),
        Paragraph(f"For <b>{settings.legal_name or '—'}</b><br/><br/><br/>"
                  f"Authorised Signatory", styles["right"]),
    ]], colWidths=[105 * mm, 70 * mm])
    receipt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(receipt)
    return _build(path, story, f"Delivery Challan {fulfillment.number}")


# --- Purchase order -----------------------------------------------------------------

def purchase_order(order, path=None) -> str:
    settings = company()
    supplier = order.supplier
    styles = _styles()
    path = path or output_dir() / f"{safe_name(order.number)}.pdf"

    story = []
    _header(story, styles, settings, "PURCHASE ORDER")

    parties = Table([[Paragraph("SUPPLIER", styles["label"]),
                      Paragraph("DELIVER TO", styles["label"])],
                     [Paragraph(_address_block(
                         supplier.name, supplier.gstin, supplier.state_code,
                         [supplier.address_line1, supplier.address_line2,
                          ", ".join(filter(None, [supplier.city, supplier.pincode]))],
                         supplier.phone, supplier.email), styles["body"]),
                      Paragraph(_address_block(
                          order.warehouse.name, settings.gstin,
                          order.warehouse.state_code,
                          [order.warehouse.address_line1,
                           ", ".join(filter(None, [order.warehouse.city,
                                                   order.warehouse.pincode]))],
                          order.warehouse.phone), styles["body"])]],
                    colWidths=[87.5 * mm, 87.5 * mm])
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ]))
    story.append(parties)
    story.append(Spacer(1, 4 * mm))

    _meta_row(story, styles, [
        ("ORDER NO.", order.number),
        ("DATE", order.order_date.strftime("%d %b %Y")),
        ("EXPECTED BY", order.expected_date.strftime("%d %b %Y")
         if order.expected_date else "—"),
        ("PAYMENT TERMS", f"{supplier.payment_terms_days} days"
         if supplier.payment_terms_days else "—"),
    ])

    interstate = gst.is_interstate(order.warehouse.state_code, supplier.state_code)
    rows = []
    for index, line in enumerate(order.lines, start=1):
        taxable = _dec(line.line_total)
        cgst, sgst, igst = gst.split_tax(taxable, line.gst_rate, interstate)
        rows.append([
            str(index),
            Paragraph(f"{line.item.name}<br/><font size=7 color='#6B7280'>"
                      f"{line.item.sku}</font>", styles["cell"]),
            line.hsn_code or "—",
            quantity(line.quantity),
            money(line.unit_cost),
            money(taxable),
            f"{_dec(line.gst_rate):g}%",
            money(taxable + cgst + sgst + igst),
        ])
    story.append(_line_table(
        ["#", "Item", "HSN", "Qty", "Rate", "Value", "GST", "Total"],
        rows,
        [10 * mm, 58 * mm, 16 * mm, 17 * mm, 22 * mm, 22 * mm, 13 * mm, 22 * mm],
        ["CENTER", "LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT", "CENTER", "RIGHT"]))
    story.append(Spacer(1, 4 * mm))

    _totals(story, styles, [
        ("Value of Goods", money(order.subtotal)),
        ("GST (" + ("IGST" if interstate else "CGST+SGST") + ")",
         money(order.tax_total)),
        ("Order Total (INR)", money(order.total)),
    ])
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"<b>Amount in words:</b> {amount_in_words(order.total)}",
                           styles["body"]))

    if order.notes:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"<b>Notes</b><br/>{order.notes}", styles["small"]))

    _signature(story, styles, settings)
    return _build(path, story, f"Purchase Order {order.number}")


# --- Goods receipt note -------------------------------------------------------------

def goods_receipt_note(receipt, path=None) -> str:
    """What actually arrived versus what was ordered — signed off at the dock."""
    settings = company()
    styles = _styles()
    path = path or output_dir() / f"{safe_name(receipt.number)}.pdf"

    story = []
    _header(story, styles, settings, "GOODS RECEIPT NOTE")

    _meta_row(story, styles, [
        ("GRN NO.", receipt.number),
        ("DATE", receipt.receipt_date.strftime("%d %b %Y")),
        ("SUPPLIER", receipt.supplier.name if receipt.supplier else "—"),
        ("WAREHOUSE", receipt.warehouse.name),
        ("PURCHASE ORDER", receipt.order.number if receipt.order else "—"),
        ("SUPPLIER INVOICE", (receipt.order.supplier_invoice_no
                              if receipt.order else None) or "—"),
        ("TRUCK / VEHICLE", receipt.truck_hsrp or "—"),
        ("RECEIVED BY", receipt.received_by.full_name if receipt.received_by else "—"),
    ])

    rows = []
    for index, line in enumerate(receipt.lines, start=1):
        ordered = _dec(line.order_line.quantity) if line.order_line else None
        rows.append([
            str(index),
            Paragraph(f"{line.item.name}<br/><font size=7 color='#6B7280'>"
                      f"{line.item.sku}</font>", styles["cell"]),
            quantity(ordered) if ordered is not None else "—",
            quantity(line.quantity),
            quantity(line.rejected_quantity) if _dec(line.rejected_quantity) else "—",
            line.lot.lot_number if line.lot else "—",
            (line.lot.expiry_date.strftime("%d %b %Y")
             if line.lot and line.lot.expiry_date else "—"),
        ])
    story.append(_line_table(
        ["#", "Item", "Ordered", "Received", "Rejected", "Batch", "Expiry"],
        rows,
        [10 * mm, 58 * mm, 20 * mm, 20 * mm, 20 * mm, 24 * mm, 23 * mm],
        ["CENTER", "LEFT", "RIGHT", "RIGHT", "RIGHT", "CENTER", "CENTER"]))

    if receipt.notes:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"<b>Notes</b><br/>{receipt.notes}", styles["small"]))

    story.append(Spacer(1, 12 * mm))
    signoff = Table([[
        Paragraph("Checked by<br/><br/><br/>_______________________", styles["small"]),
        Paragraph("Stored by<br/><br/><br/>_______________________", styles["small"]),
        Paragraph("Approved by<br/><br/><br/>_______________________", styles["small"]),
    ]], colWidths=[58 * mm, 58 * mm, 59 * mm])
    signoff.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(signoff)
    return _build(path, story, f"Goods Receipt Note {receipt.number}")


# --- Barcode labels -----------------------------------------------------------------

def barcode_labels(items, path=None, copies=1, include_price=True) -> str:
    """A sheet of Code128 labels, 3 across, for items or shelf edges."""
    import barcode
    from barcode.writer import ImageWriter

    settings = company()
    styles = _styles()
    path = path or output_dir() / f"labels-{datetime.date.today():%Y%m%d-%H%M%S}.pdf"
    # Keep intermediates beside the requested output. Web downloads pass a path in the
    # platform temp directory, which is the only writable location on serverless hosts.
    temp_dir = Path(tempfile.mkdtemp(
        prefix="ims_barcodes_", dir=str(Path(path).parent)))

    story = [Paragraph("Item Labels", styles["title"]),
             Paragraph(f"{settings.legal_name or ''} · "
                       f"{datetime.date.today():%d %b %Y}", styles["small"]),
             Spacer(1, 5 * mm)]

    cells = []
    for item in items:
        code = (item.barcode or item.sku or "").strip()
        if not code:
            continue
        try:
            writer = ImageWriter()
            symbol = barcode.get("code128", code, writer=writer)
            filename = temp_dir / f"{safe_name(item.sku)}"
            image_path = symbol.save(str(filename), options={
                "module_height": 9.0, "font_size": 7, "text_distance": 2.0,
                "quiet_zone": 2.0, "dpi": 300,
            })
        except Exception as exc:
            log.warning("Could not render a barcode for %s: %s", item.sku, exc)
            continue

        caption = [f"<b>{item.name[:38]}</b>", item.sku]
        if include_price and _dec(item.selling_price) > 0:
            caption.append(f"MRP {money(item.selling_price)}")
        cell = [Image(image_path, width=48 * mm, height=20 * mm),
                Paragraph("<br/>".join(caption), styles["small"])]
        for _ in range(max(1, int(copies))):
            cells.append(cell)

    if not cells:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DocumentError(
            "None of the selected items have a barcode or SKU to print.")

    rows = [cells[i:i + 3] for i in range(0, len(cells), 3)]
    if rows and len(rows[-1]) < 3:
        rows[-1] += [""] * (3 - len(rows[-1]))

    table = Table(rows, colWidths=[58.3 * mm] * 3)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)

    try:
        return _build(path, story, "Item Labels")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
