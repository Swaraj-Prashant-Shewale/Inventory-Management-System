"""Global search across every record type the header search bar can reach.

Ordered so the most likely intent wins: an exact barcode or SKU (someone has scanned
something) beats an exact document number, which beats a partial name match.
"""
from dataclasses import dataclass
from typing import Any

from peewee import JOIN

from database.models import (
    Customer,
    CustomerReturn,
    Employee,
    Item,
    PurchaseOrder,
    PurchaseStatus,
    SalesOrder,
    SalesStatus,
    Supplier,
    SupplierReturn,
    Warehouse,
)

ITEM = "item"
SALES_ORDER = "sales_order"
PURCHASE_ORDER = "purchase_order"
CUSTOMER_RETURN = "customer_return"
SUPPLIER_RETURN = "supplier_return"
CUSTOMER = "customer"
SUPPLIER = "supplier"
EMPLOYEE = "employee"

TYPE_LABELS = {
    ITEM: "Item",
    SALES_ORDER: "Sales Order",
    PURCHASE_ORDER: "Purchase Order",
    CUSTOMER_RETURN: "Customer Return",
    SUPPLIER_RETURN: "Supplier Return",
    CUSTOMER: "Customer",
    SUPPLIER: "Supplier",
    EMPLOYEE: "Employee",
}


@dataclass
class Result:
    kind: str
    label: str
    detail: str
    payload: Any
    exact: bool = False

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.kind, self.kind)


def scanned_item(term):
    """An exact barcode or SKU match — treat as a scan and go straight there."""
    term = (term or "").strip()
    if not term:
        return None
    return Item.get_or_none((Item.barcode == term) | (Item.sku == term.upper()))


def search(term, limit_per_type=6, include_inactive=False):
    """Everything matching `term`, most relevant first."""
    term = (term or "").strip()
    if len(term) < 2:
        return []

    like = f"%{term}%"
    upper = term.upper()
    results = []

    # --- Items ----------------------------------------------------------------------
    items = (Item.select(Item, Supplier)
             .join(Supplier, JOIN.LEFT_OUTER, on=Item.preferred_supplier)
             .where((Item.name ** like) | (Item.sku ** like) | (Item.barcode ** like)))
    if not include_inactive:
        items = items.where(Item.is_active == True)  # noqa: E712
    for item in items.order_by(Item.name).limit(limit_per_type):
        exact = item.sku.upper() == upper or (item.barcode or "") == term
        results.append(Result(
            ITEM, item.name,
            f"{item.sku}"
            + (f" · barcode {item.barcode}" if item.barcode else "")
            + (f" · {item.preferred_supplier.name}" if item.preferred_supplier else ""),
            item, exact))

    # --- Sales orders ---------------------------------------------------------------
    orders = (SalesOrder.select(SalesOrder, Customer, Warehouse)
              .join(Customer).switch(SalesOrder).join(Warehouse)
              .where((SalesOrder.number ** like)
                     | (Customer.name ** like)
                     | (SalesOrder.truck_hsrp ** like)))
    for order in orders.order_by(SalesOrder.id.desc()).limit(limit_per_type):
        results.append(Result(
            SALES_ORDER, order.number,
            f"{order.customer.name} · "
            f"{SalesStatus.LABELS.get(order.status, order.status)} · "
            f"{order.warehouse.name}",
            order, order.number.upper() == upper))

    # --- Purchase orders ------------------------------------------------------------
    purchases = (PurchaseOrder.select(PurchaseOrder, Supplier, Warehouse)
                 .join(Supplier).switch(PurchaseOrder).join(Warehouse)
                 .where((PurchaseOrder.number ** like)
                        | (Supplier.name ** like)
                        | (PurchaseOrder.supplier_invoice_no ** like)))
    for order in purchases.order_by(PurchaseOrder.id.desc()).limit(limit_per_type):
        results.append(Result(
            PURCHASE_ORDER, order.number,
            f"{order.supplier.name} · "
            f"{PurchaseStatus.LABELS.get(order.status, order.status)} · "
            f"{order.warehouse.name}",
            order, order.number.upper() == upper))

    # --- Returns --------------------------------------------------------------------
    customer_returns = (CustomerReturn.select(CustomerReturn, Customer, Warehouse)
                        .join(Customer).switch(CustomerReturn).join(Warehouse)
                        .where((CustomerReturn.number ** like)
                               | (Customer.name ** like)
                               | (CustomerReturn.reason ** like)))
    for record in customer_returns.order_by(CustomerReturn.id.desc()).limit(limit_per_type):
        results.append(Result(
            CUSTOMER_RETURN, record.number,
            f"{record.customer.name} - "
            f"{'Restocked' if record.restock else 'Scrapped'} - "
            f"{record.warehouse.name}",
            record, record.number.upper() == upper))

    supplier_returns = (SupplierReturn.select(SupplierReturn, Supplier, Warehouse)
                        .join(Supplier).switch(SupplierReturn).join(Warehouse)
                        .where((SupplierReturn.number ** like)
                               | (Supplier.name ** like)
                               | (SupplierReturn.reason ** like)))
    for record in supplier_returns.order_by(SupplierReturn.id.desc()).limit(limit_per_type):
        results.append(Result(
            SUPPLIER_RETURN, record.number,
            f"{record.supplier.name} - Returned - {record.warehouse.name}",
            record, record.number.upper() == upper))
    # --- Partners and people --------------------------------------------------------
    customers = Customer.select().where(
        (Customer.name ** like) | (Customer.code ** like) | (Customer.gstin ** like)
        | (Customer.phone ** like))
    if not include_inactive:
        customers = customers.where(Customer.is_active == True)  # noqa: E712
    for customer in customers.order_by(Customer.name).limit(limit_per_type):
        results.append(Result(
            CUSTOMER, customer.name,
            f"{customer.code}"
            + (f" · GSTIN {customer.gstin}" if customer.gstin else "")
            + (f" · {customer.phone}" if customer.phone else ""),
            customer, customer.code.upper() == upper))

    suppliers = Supplier.select().where(
        (Supplier.name ** like) | (Supplier.code ** like) | (Supplier.gstin ** like)
        | (Supplier.phone ** like))
    if not include_inactive:
        suppliers = suppliers.where(Supplier.is_active == True)  # noqa: E712
    for supplier in suppliers.order_by(Supplier.name).limit(limit_per_type):
        results.append(Result(
            SUPPLIER, supplier.name,
            f"{supplier.code}"
            + (f" · GSTIN {supplier.gstin}" if supplier.gstin else "")
            + (f" · lead time {supplier.lead_time_days} days"
               if supplier.lead_time_days else ""),
            supplier, supplier.code.upper() == upper))

    employees = (Employee.select(Employee, Warehouse)
                 .join(Warehouse, JOIN.LEFT_OUTER)
                 .where((Employee.name ** like) | (Employee.code ** like)
                        | (Employee.phone ** like)))
    if not include_inactive:
        employees = employees.where(Employee.is_active == True)  # noqa: E712
    for employee in employees.order_by(Employee.name).limit(limit_per_type):
        results.append(Result(
            EMPLOYEE, employee.name,
            f"badge {employee.code}"
            + (f" · {employee.designation}" if employee.designation else "")
            + (f" · {employee.warehouse.name}" if employee.warehouse else ""),
            employee, employee.code.upper() == upper))

    # Exact matches first, then by type in the order defined above.
    order_index = {kind: index for index, kind in enumerate(
        [ITEM, SALES_ORDER, PURCHASE_ORDER, CUSTOMER_RETURN, SUPPLIER_RETURN,
         CUSTOMER, SUPPLIER, EMPLOYEE])}
    results.sort(key=lambda r: (not r.exact, order_index.get(r.kind, 99),
                                r.label.lower()))
    return results


def summarise(results) -> str:
    """'3 items · 1 customer' — for the results header."""
    counts = {}
    for result in results:
        counts[result.kind] = counts.get(result.kind, 0) + 1
    parts = []
    for kind, count in counts.items():
        label = TYPE_LABELS.get(kind, kind).lower()
        parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    return " · ".join(parts)
