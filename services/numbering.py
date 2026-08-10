"""Document numbering: PO/25-26/0001 and friends.

Numbers are handed out inside a transaction with a row lock on Postgres, so two warehouse
PCs pressing Create at the same instant can't collide on the same number.
"""
import datetime

import config
from database.connection import db, is_postgres
from database.models import CompanySettings, NumberSequence

# doc_type -> (prefix, includes financial year)
DEFAULT_SEQUENCES = {
    "PURCHASE_ORDER": ("PO", True),
    "GOODS_RECEIPT": ("GRN", True),
    "SALES_ORDER": ("SO", True),
    "FULFILLMENT": ("FUL", True),
    "INVOICE": ("INV", True),
    "PAYMENT": ("PAY", True),
    "STOCK_ADJUSTMENT": ("ADJ", True),
    "STOCK_TRANSFER": ("TRF", True),
    "CYCLE_COUNT": ("CNT", True),
    "CUSTOMER_RETURN": ("CRN", True),
    "SUPPLIER_RETURN": ("SRN", True),
    "ITEM": ("SKU", False),
    "SUPPLIER": ("SUP", False),
    "CUSTOMER": ("CUS", False),
    "EMPLOYEE": ("EMP", False),
}


def financial_year_label(on_date=None, start_month=None) -> str:
    """'25-26' for the Indian financial year containing `on_date`."""
    on_date = on_date or datetime.date.today()
    if start_month is None:
        settings = CompanySettings.get_or_none()
        start_month = settings.fy_start_month if settings else config.FINANCIAL_YEAR_START_MONTH
    start_year = on_date.year if on_date.month >= start_month else on_date.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def ensure_sequences():
    """Create any missing sequence rows. Safe to run on every startup."""
    for doc_type, (prefix, include_fy) in DEFAULT_SEQUENCES.items():
        NumberSequence.get_or_create(
            doc_type=doc_type,
            defaults={"prefix": prefix, "next_number": 1, "padding": 4,
                      "include_fy": include_fy},
        )


def next_number(doc_type: str, on_date=None) -> str:
    """Reserve and return the next number for `doc_type`."""
    with db.atomic():
        query = NumberSequence.select().where(NumberSequence.doc_type == doc_type)
        if is_postgres():
            query = query.for_update()
        seq = query.first()

        if seq is None:
            prefix, include_fy = DEFAULT_SEQUENCES.get(doc_type, (doc_type[:3].upper(), True))
            seq = NumberSequence.create(
                doc_type=doc_type, prefix=prefix, next_number=1, padding=4,
                include_fy=include_fy,
            )

        fy = financial_year_label(on_date) if seq.include_fy else None
        # Restart the counter when a new financial year begins, so each FY forms its own
        # 0001.. series rather than one counter running across years.
        if fy is not None and seq.last_fy != fy:
            seq.next_number = 1
            seq.last_fy = fy

        number = seq.next_number
        seq.next_number = number + 1
        seq.save()

        parts = [seq.prefix]
        if fy is not None:
            parts.append(fy)
        parts.append(str(number).zfill(seq.padding))
        return "/".join(parts)


def peek_number(doc_type: str, on_date=None) -> str:
    """What `next_number` would return, without consuming it. For preview labels only."""
    seq = NumberSequence.get_or_none(NumberSequence.doc_type == doc_type)
    if seq is None:
        prefix, include_fy = DEFAULT_SEQUENCES.get(doc_type, (doc_type[:3].upper(), True))
        padding, number, last_fy = 4, 1, None
    else:
        prefix, include_fy, padding, number, last_fy = (
            seq.prefix, seq.include_fy, seq.padding, seq.next_number, seq.last_fy,
        )
    parts = [prefix]
    if include_fy:
        fy = financial_year_label(on_date)
        # Reflect the FY-rollover reset the real allocation will perform.
        if last_fy != fy:
            number = 1
        parts.append(fy)
    parts.append(str(number).zfill(padding))
    return "/".join(parts)
