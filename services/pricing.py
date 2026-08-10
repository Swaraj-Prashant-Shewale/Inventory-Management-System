"""Selling price resolution.

A customer's price comes from their price list: an explicit per-item price if one exists
(honouring quantity breaks), otherwise the list's blanket discount off the item's base
price. No list, no match — the item's own selling price stands.
"""
from decimal import Decimal

from database.models import ZERO, PriceList, PriceListItem

TWO_PLACES = Decimal("0.01")


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def default_price_list():
    return PriceList.get_or_none(
        (PriceList.is_default == True) & (PriceList.is_active == True)  # noqa: E712
    )


def resolve_price(item, customer=None, quantity=None) -> Decimal:
    """The unit price this customer pays for this item at this quantity."""
    base = _dec(item.selling_price)
    price_list = customer.price_list if customer is not None else None
    if price_list is None or not price_list.is_active:
        return base.quantize(TWO_PLACES)

    quantity = _dec(quantity) if quantity is not None else ZERO

    # Quantity breaks: take the highest threshold this order actually reaches.
    match = (
        PriceListItem.select()
        .where(
            (PriceListItem.price_list == price_list)
            & (PriceListItem.item == item)
            & (PriceListItem.min_quantity <= quantity)
        )
        .order_by(PriceListItem.min_quantity.desc())
        .first()
    )
    if match is None:
        # No break reached — fall back to the entry with no minimum, if there is one.
        match = (
            PriceListItem.select()
            .where(
                (PriceListItem.price_list == price_list)
                & (PriceListItem.item == item)
                & (PriceListItem.min_quantity == ZERO)
            )
            .first()
        )

    if match is not None:
        return _dec(match.price).quantize(TWO_PLACES)

    discount = _dec(price_list.discount_percent)
    if discount > 0:
        return (base * (Decimal("100") - discount) / Decimal("100")).quantize(TWO_PLACES)

    return base.quantize(TWO_PLACES)


def price_explanation(item, customer=None, quantity=None) -> str:
    """Short human-readable reason for the price, shown as a tooltip on order lines."""
    price_list = customer.price_list if customer is not None else None
    if price_list is None or not price_list.is_active:
        return "Item list price"

    quantity = _dec(quantity) if quantity is not None else ZERO
    match = (
        PriceListItem.select()
        .where(
            (PriceListItem.price_list == price_list)
            & (PriceListItem.item == item)
            & (PriceListItem.min_quantity <= quantity)
        )
        .order_by(PriceListItem.min_quantity.desc())
        .first()
    )
    if match is not None:
        if _dec(match.min_quantity) > 0:
            return (f"{price_list.name} price from {match.min_quantity:f} units")
        return f"{price_list.name} price"

    discount = _dec(price_list.discount_percent)
    if discount > 0:
        return f"{price_list.name}: {discount:f}% off list"
    return f"{price_list.name} (no special price — list price used)"


def set_price(price_list, item, price, min_quantity=ZERO):
    """Create or update one entry on a price list."""
    entry, created = PriceListItem.get_or_create(
        price_list=price_list, item=item, min_quantity=_dec(min_quantity),
        defaults={"price": _dec(price)},
    )
    if not created:
        entry.price = _dec(price)
        entry.save()
    return entry
