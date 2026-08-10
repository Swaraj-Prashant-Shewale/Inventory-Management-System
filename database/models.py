"""The complete data model.

Stock is tracked per warehouse (StockLevel), orders carry line items, item cost follows a
moving average recalculated on every receipt, and every quantity change writes a row to
StockMovement — which is what the Recent Logs screen reads.

Money uses DECIMAL rather than float throughout; rounding errors in a ledger are bugs.
"""
import datetime
from decimal import Decimal

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)

from database.connection import db

ZERO = Decimal("0")


# --- Field helpers ------------------------------------------------------------------

def _decimal_field(digits, places, kwargs):
    """Shared construction for the money/quantity/rate helpers.

    A nullable column gets no zero default: NULL has to stay distinguishable from 0, or
    'no override set' becomes indistinguishable from 'the override is zero'.
    """
    kwargs.setdefault("max_digits", digits)
    kwargs.setdefault("decimal_places", places)
    kwargs.setdefault("auto_round", True)
    if not kwargs.get("null"):
        kwargs.setdefault("default", ZERO)
    return DecimalField(**kwargs)


def MoneyField(**kwargs):
    return _decimal_field(16, 2, kwargs)


def CostField(**kwargs):
    """Cost basis (moving average, frozen COGS) at 6 decimals.

    A weighted-average recompute routinely lands on sub-cent values, and the stored basis
    feeds the next receipt's blend — rounding to the cent between receipts compounds
    valuation and COGS drift, which is a bug in a ledger. Invoiced money stays 2dp; this
    is only the internal cost basis.
    """
    return _decimal_field(18, 6, kwargs)


def QtyField(**kwargs):
    """Quantities allow 3 decimals so kg/litre items behave."""
    return _decimal_field(16, 3, kwargs)


def RateField(**kwargs):
    """Percentages such as GST rates."""
    return _decimal_field(6, 2, kwargs)


# --- Enumerations -------------------------------------------------------------------

class Role:
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    CLERK = "CLERK"
    ALL = (OWNER, MANAGER, CLERK)
    LABELS = {OWNER: "Owner", MANAGER: "Manager", CLERK: "Clerk"}


class PurchaseStatus:
    DRAFT = "DRAFT"
    ORDERED = "ORDERED"
    PARTIAL = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"
    OPEN = (DRAFT, ORDERED, PARTIAL)
    LABELS = {
        DRAFT: "Draft",
        ORDERED: "Ordered",
        PARTIAL: "Partially Received",
        RECEIVED: "Received",
        CANCELLED: "Cancelled",
    }


class SalesStatus:
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIALLY_SHIPPED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"
    OPEN = (DRAFT, CONFIRMED, PARTIAL)
    LABELS = {
        DRAFT: "Draft",
        CONFIRMED: "Confirmed",
        PARTIAL: "Partially Shipped",
        SHIPPED: "Shipped",
        CANCELLED: "Cancelled",
    }


class PaymentStatus:
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"
    LABELS = {UNPAID: "Unpaid", PARTIAL: "Partial", PAID: "Paid"}


class Direction:
    IN = "IN"
    OUT = "OUT"


class DocType:
    """Source document for a stock movement."""
    GOODS_RECEIPT = "GRN"
    FULFILLMENT = "FULFILLMENT"
    ADJUSTMENT = "ADJUSTMENT"
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    CYCLE_COUNT = "CYCLE_COUNT"
    CUSTOMER_RETURN = "CUSTOMER_RETURN"
    SUPPLIER_RETURN = "SUPPLIER_RETURN"
    OPENING = "OPENING"
    LABELS = {
        GOODS_RECEIPT: "Goods Receipt",
        FULFILLMENT: "Fulfilment",
        ADJUSTMENT: "Adjustment",
        TRANSFER_OUT: "Transfer Out",
        TRANSFER_IN: "Transfer In",
        CYCLE_COUNT: "Cycle Count",
        CUSTOMER_RETURN: "Customer Return",
        SUPPLIER_RETURN: "Supplier Return",
        OPENING: "Opening Balance",
    }


class TransferStatus:
    DRAFT = "DRAFT"
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"
    LABELS = {
        DRAFT: "Draft",
        IN_TRANSIT: "In Transit",
        RECEIVED: "Received",
        CANCELLED: "Cancelled",
    }


class CountStatus:
    OPEN = "OPEN"
    COUNTED = "COUNTED"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    LABELS = {
        OPEN: "Open",
        COUNTED: "Counted",
        APPROVED: "Approved",
        CANCELLED: "Cancelled",
    }


class TaskStatus:
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"
    ACTIVE = (PENDING, IN_PROGRESS)
    LABELS = {
        PENDING: "Pending",
        IN_PROGRESS: "In Progress",
        DONE: "Done",
        CANCELLED: "Cancelled",
    }


class ReminderSource:
    MANUAL = "MANUAL"
    LOW_STOCK = "LOW_STOCK"
    PO_OVERDUE = "PO_OVERDUE"
    LOT_EXPIRY = "LOT_EXPIRY"
    LABELS = {
        MANUAL: "Manual",
        LOW_STOCK: "Below Minimum",
        PO_OVERDUE: "Purchase Order Overdue",
        LOT_EXPIRY: "Lot Expiring",
    }


class SerialStatus:
    IN_STOCK = "IN_STOCK"
    SHIPPED = "SHIPPED"
    RETURNED = "RETURNED"
    SCRAPPED = "SCRAPPED"


class AdjustmentReason:
    DAMAGE = "DAMAGE"
    LOSS = "LOSS"
    THEFT = "THEFT"
    FOUND = "FOUND"
    EXPIRY = "EXPIRY"
    COUNT = "COUNT_VARIANCE"
    OTHER = "OTHER"
    LABELS = {
        DAMAGE: "Damaged",
        LOSS: "Lost",
        THEFT: "Theft",
        FOUND: "Found / Surplus",
        EXPIRY: "Expired",
        COUNT: "Count Variance",
        OTHER: "Other",
    }
    ALL = (DAMAGE, LOSS, THEFT, FOUND, EXPIRY, COUNT, OTHER)


# --- Base ---------------------------------------------------------------------------

class BaseModel(Model):
    id = AutoField()

    class Meta:
        database = db
        legacy_table_names = False


class TimestampedModel(BaseModel):
    created_at = DateTimeField(default=datetime.datetime.now)
    updated_at = DateTimeField(default=datetime.datetime.now)

    def save(self, *args, **kwargs):
        self.updated_at = datetime.datetime.now()
        return super().save(*args, **kwargs)


# --- Company & configuration --------------------------------------------------------

class CompanySettings(TimestampedModel):
    """Single row. Supplies the header on every GST invoice."""
    legal_name = CharField(max_length=200, default="My Company")
    trade_name = CharField(max_length=200, null=True)
    gstin = CharField(max_length=15, null=True)
    pan = CharField(max_length=10, null=True)
    address_line1 = CharField(max_length=200, null=True)
    address_line2 = CharField(max_length=200, null=True)
    city = CharField(max_length=100, null=True)
    state = CharField(max_length=100, null=True)
    state_code = CharField(max_length=2, null=True)  # GST state code, e.g. "27"
    pincode = CharField(max_length=10, null=True)
    phone = CharField(max_length=40, null=True)
    email = CharField(max_length=120, null=True)
    logo_path = CharField(max_length=400, null=True)
    bank_name = CharField(max_length=120, null=True)
    bank_account = CharField(max_length=40, null=True)
    bank_ifsc = CharField(max_length=20, null=True)
    fy_start_month = IntegerField(default=4)
    currency_symbol = CharField(max_length=5, default="₹")
    invoice_terms = TextField(null=True)


class NumberSequence(BaseModel):
    """Per-document-type running numbers, e.g. PO/25-26/0001."""
    doc_type = CharField(max_length=40, unique=True)
    prefix = CharField(max_length=20)
    next_number = IntegerField(default=1)
    padding = IntegerField(default=4)
    include_fy = BooleanField(default=True)
    # The financial year the counter last advanced in; when it rolls over, an FY-tagged
    # series restarts at 1 (INV/25-26/0453 -> INV/26-27/0001), as accountants expect.
    last_fy = CharField(max_length=8, null=True)


# --- People -------------------------------------------------------------------------

class Warehouse(TimestampedModel):
    code = CharField(max_length=20, unique=True)
    name = CharField(max_length=120)
    address_line1 = CharField(max_length=200, null=True)
    city = CharField(max_length=100, null=True)
    state = CharField(max_length=100, null=True)
    state_code = CharField(max_length=2, null=True)  # drives CGST/SGST vs IGST
    pincode = CharField(max_length=10, null=True)
    phone = CharField(max_length=40, null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class Employee(TimestampedModel):
    code = CharField(max_length=30, unique=True)       # printed on the ID badge barcode
    name = CharField(max_length=120)
    designation = CharField(max_length=80, null=True)
    warehouse = ForeignKeyField(Warehouse, backref="employees", null=True,
                                on_delete="SET NULL")
    phone = CharField(max_length=40, null=True)
    email = CharField(max_length=120, null=True)
    joined_on = DateField(null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class User(TimestampedModel):
    """Login credentials. Employees without a login simply have no User row."""
    username = CharField(max_length=60, unique=True)
    password_hash = CharField(max_length=255)
    full_name = CharField(max_length=120)
    role = CharField(max_length=20, default=Role.CLERK)
    employee = ForeignKeyField(Employee, backref="logins", null=True,
                               on_delete="SET NULL")
    warehouse = ForeignKeyField(Warehouse, backref="users", null=True,
                                on_delete="SET NULL")
    is_active = BooleanField(default=True)
    must_change_password = BooleanField(default=False)
    last_login_at = DateTimeField(null=True)
    failed_attempts = IntegerField(default=0)
    locked_until = DateTimeField(null=True)

    @property
    def role_label(self):
        return Role.LABELS.get(self.role, self.role)

    def __str__(self):
        return self.full_name


# --- Item master --------------------------------------------------------------------

class Category(TimestampedModel):
    name = CharField(max_length=120, unique=True)
    parent = ForeignKeyField("self", backref="children", null=True,
                             on_delete="SET NULL")
    description = TextField(null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class Uom(TimestampedModel):
    """Unit of measure. `allow_decimal` blocks half-a-carton nonsense on discrete units."""
    code = CharField(max_length=15, unique=True)
    name = CharField(max_length=60)
    allow_decimal = BooleanField(default=False)

    def __str__(self):
        return self.code


class Supplier(TimestampedModel):
    code = CharField(max_length=30, unique=True)
    name = CharField(max_length=160)
    gstin = CharField(max_length=15, null=True)
    state = CharField(max_length=100, null=True)
    state_code = CharField(max_length=2, null=True)
    contact_person = CharField(max_length=120, null=True)
    phone = CharField(max_length=40, null=True)
    email = CharField(max_length=120, null=True)
    address_line1 = CharField(max_length=200, null=True)
    address_line2 = CharField(max_length=200, null=True)
    city = CharField(max_length=100, null=True)
    pincode = CharField(max_length=10, null=True)
    lead_time_days = IntegerField(default=7)          # feeds the Delays widget
    payment_terms_days = IntegerField(default=30)
    notes = TextField(null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class PriceList(TimestampedModel):
    """A customer pricing tier — Retail, Wholesale, Contract."""
    name = CharField(max_length=80, unique=True)
    description = CharField(max_length=200, null=True)
    is_default = BooleanField(default=False)
    discount_percent = RateField(default=ZERO)   # fallback when no explicit item price
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class Customer(TimestampedModel):
    code = CharField(max_length=30, unique=True)
    name = CharField(max_length=160)
    gstin = CharField(max_length=15, null=True)
    state = CharField(max_length=100, null=True)
    state_code = CharField(max_length=2, null=True)
    contact_person = CharField(max_length=120, null=True)
    phone = CharField(max_length=40, null=True)
    email = CharField(max_length=120, null=True)
    billing_address = TextField(null=True)
    delivery_address = TextField(null=True)
    delivery_pincode = CharField(max_length=10, null=True)   # "Delivery Postcode" column
    price_list = ForeignKeyField(PriceList, backref="customers", null=True,
                                 on_delete="SET NULL")
    payment_terms_days = IntegerField(default=30)
    credit_limit = MoneyField(default=ZERO)
    notes = TextField(null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return self.name


class Item(TimestampedModel):
    sku = CharField(max_length=40, unique=True)
    barcode = CharField(max_length=60, null=True, index=True)
    name = CharField(max_length=200, index=True)
    description = TextField(null=True)
    category = ForeignKeyField(Category, backref="items", null=True,
                               on_delete="SET NULL")

    base_uom = ForeignKeyField(Uom, backref="base_items", null=True,
                               on_delete="SET NULL")
    purchase_uom = ForeignKeyField(Uom, backref="purchase_items", null=True,
                                   on_delete="SET NULL")
    pack_size = QtyField(default=Decimal("1"))   # base units per purchase unit

    hsn_code = CharField(max_length=10, null=True)
    gst_rate = RateField(default=Decimal("18"))

    # Defaults; a StockLevel row may override per warehouse.
    default_min_level = QtyField(default=ZERO)
    default_max_level = QtyField(default=ZERO)

    avg_cost = CostField(default=ZERO)           # moving average, maintained on receipt
    last_purchase_cost = CostField(default=ZERO)
    selling_price = MoneyField(default=ZERO)     # base price before price lists

    preferred_supplier = ForeignKeyField(Supplier, backref="preferred_items", null=True,
                                         on_delete="SET NULL")

    is_lot_tracked = BooleanField(default=False)
    is_serial_tracked = BooleanField(default=False)
    shelf_life_days = IntegerField(null=True)
    is_active = BooleanField(default=True)

    def __str__(self):
        return f"{self.sku} — {self.name}"


class PriceListItem(TimestampedModel):
    price_list = ForeignKeyField(PriceList, backref="prices", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="list_prices", on_delete="CASCADE")
    price = MoneyField()
    min_quantity = QtyField(default=ZERO)

    class Meta:
        indexes = ((("price_list", "item", "min_quantity"), True),)


# --- Stock --------------------------------------------------------------------------

class StockLevel(TimestampedModel):
    """On-hand quantity of one item in one warehouse. The heart of the stock model."""
    item = ForeignKeyField(Item, backref="stock_levels", on_delete="CASCADE")
    warehouse = ForeignKeyField(Warehouse, backref="stock_levels", on_delete="CASCADE")
    on_hand = QtyField(default=ZERO)
    reserved = QtyField(default=ZERO)     # committed to confirmed, unshipped sales orders
    min_level = QtyField(null=True)       # null → fall back to the item default
    max_level = QtyField(null=True)

    class Meta:
        indexes = ((("item", "warehouse"), True),)

    @property
    def available(self):
        return (self.on_hand or ZERO) - (self.reserved or ZERO)

    @property
    def effective_min(self):
        return self.min_level if self.min_level is not None else self.item.default_min_level

    @property
    def effective_max(self):
        return self.max_level if self.max_level is not None else self.item.default_max_level


class Lot(TimestampedModel):
    """A batch of a lot-tracked item, with its expiry date. Picked FEFO."""
    item = ForeignKeyField(Item, backref="lots", on_delete="CASCADE")
    lot_number = CharField(max_length=60)
    expiry_date = DateField(null=True)
    manufactured_date = DateField(null=True)
    received_date = DateField(null=True)
    supplier = ForeignKeyField(Supplier, backref="lots", null=True, on_delete="SET NULL")
    unit_cost = MoneyField(default=ZERO)

    class Meta:
        indexes = ((("item", "lot_number"), True),)

    def __str__(self):
        return self.lot_number


class LotStock(TimestampedModel):
    lot = ForeignKeyField(Lot, backref="stock", on_delete="CASCADE")
    warehouse = ForeignKeyField(Warehouse, backref="lot_stock", on_delete="CASCADE")
    quantity = QtyField(default=ZERO)

    class Meta:
        indexes = ((("lot", "warehouse"), True),)


class Serial(TimestampedModel):
    """One physical unit of a serial-tracked item."""
    item = ForeignKeyField(Item, backref="serials", on_delete="CASCADE")
    serial_number = CharField(max_length=80)
    lot = ForeignKeyField(Lot, backref="serials", null=True, on_delete="SET NULL")
    warehouse = ForeignKeyField(Warehouse, backref="serials", null=True,
                                on_delete="SET NULL")
    status = CharField(max_length=20, default=SerialStatus.IN_STOCK)
    unit_cost = MoneyField(default=ZERO)
    received_on = DateField(null=True)
    shipped_on = DateField(null=True)

    class Meta:
        indexes = ((("item", "serial_number"), True),)


# --- Purchasing ---------------------------------------------------------------------

class PurchaseOrder(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    supplier = ForeignKeyField(Supplier, backref="purchase_orders", on_delete="RESTRICT")
    warehouse = ForeignKeyField(Warehouse, backref="purchase_orders",
                                on_delete="RESTRICT")
    status = CharField(max_length=25, default=PurchaseStatus.DRAFT, index=True)
    order_date = DateField(default=datetime.date.today)
    expected_date = DateField(null=True)        # the "ETA" column
    received_date = DateField(null=True)
    subtotal = MoneyField(default=ZERO)
    tax_total = MoneyField(default=ZERO)
    total = MoneyField(default=ZERO)
    supplier_invoice_no = CharField(max_length=60, null=True)
    notes = TextField(null=True)
    created_by = ForeignKeyField(User, backref="purchase_orders", null=True,
                                 on_delete="SET NULL")

    @property
    def is_overdue(self):
        return (
            self.status in PurchaseStatus.OPEN
            and self.expected_date is not None
            and self.expected_date < datetime.date.today()
        )

    def __str__(self):
        return self.number


class PurchaseOrderLine(BaseModel):
    order = ForeignKeyField(PurchaseOrder, backref="lines", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="purchase_lines", on_delete="RESTRICT")
    quantity = QtyField()                    # in base units
    received_quantity = QtyField(default=ZERO)
    unit_cost = MoneyField()
    gst_rate = RateField(default=ZERO)
    hsn_code = CharField(max_length=10, null=True)
    line_total = MoneyField(default=ZERO)    # excluding tax
    notes = CharField(max_length=200, null=True)

    @property
    def outstanding(self):
        return (self.quantity or ZERO) - (self.received_quantity or ZERO)


class GoodsReceipt(TimestampedModel):
    """What actually arrived at the dock — printed as the GRN."""
    number = CharField(max_length=40, unique=True)
    order = ForeignKeyField(PurchaseOrder, backref="receipts", null=True,
                            on_delete="SET NULL")
    supplier = ForeignKeyField(Supplier, backref="receipts", null=True,
                               on_delete="SET NULL")
    warehouse = ForeignKeyField(Warehouse, backref="receipts", on_delete="RESTRICT")
    receipt_date = DateField(default=datetime.date.today)
    truck_hsrp = CharField(max_length=20, null=True)
    received_by = ForeignKeyField(User, backref="receipts", null=True,
                                  on_delete="SET NULL")
    notes = TextField(null=True)

    def __str__(self):
        return self.number


class GoodsReceiptLine(BaseModel):
    receipt = ForeignKeyField(GoodsReceipt, backref="lines", on_delete="CASCADE")
    order_line = ForeignKeyField(PurchaseOrderLine, backref="receipt_lines", null=True,
                                 on_delete="SET NULL")
    item = ForeignKeyField(Item, backref="receipt_lines", on_delete="RESTRICT")
    quantity = QtyField()
    unit_cost = MoneyField()
    lot = ForeignKeyField(Lot, backref="receipt_lines", null=True, on_delete="SET NULL")
    rejected_quantity = QtyField(default=ZERO)


class SupplierReturn(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    supplier = ForeignKeyField(Supplier, backref="returns", on_delete="RESTRICT")
    warehouse = ForeignKeyField(Warehouse, backref="supplier_returns",
                                on_delete="RESTRICT")
    order = ForeignKeyField(PurchaseOrder, backref="returns", null=True,
                            on_delete="SET NULL")
    return_date = DateField(default=datetime.date.today)
    reason = CharField(max_length=200, null=True)
    total = MoneyField(default=ZERO)
    created_by = ForeignKeyField(User, backref="supplier_returns", null=True,
                                 on_delete="SET NULL")
    notes = TextField(null=True)


class SupplierReturnLine(BaseModel):
    supplier_return = ForeignKeyField(SupplierReturn, backref="lines",
                                      on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="supplier_return_lines", on_delete="RESTRICT")
    quantity = QtyField()
    unit_cost = MoneyField(default=ZERO)
    lot = ForeignKeyField(Lot, backref="supplier_return_lines", null=True,
                          on_delete="SET NULL")


# --- Sales --------------------------------------------------------------------------

class SalesOrder(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    customer = ForeignKeyField(Customer, backref="sales_orders", on_delete="RESTRICT")
    warehouse = ForeignKeyField(Warehouse, backref="sales_orders", on_delete="RESTRICT")
    status = CharField(max_length=25, default=SalesStatus.DRAFT, index=True)
    order_date = DateField(default=datetime.date.today)
    promised_date = DateField(null=True)
    shipped_date = DateField(null=True)
    delivery_address = TextField(null=True)
    delivery_pincode = CharField(max_length=10, null=True)
    truck_hsrp = CharField(max_length=20, null=True)

    subtotal = MoneyField(default=ZERO)
    tax_total = MoneyField(default=ZERO)
    total = MoneyField(default=ZERO)
    amount_paid = MoneyField(default=ZERO)
    payment_status = CharField(max_length=15, default=PaymentStatus.UNPAID)

    notes = TextField(null=True)
    created_by = ForeignKeyField(User, backref="sales_orders", null=True,
                                 on_delete="SET NULL")

    @property
    def balance_due(self):
        return (self.total or ZERO) - (self.amount_paid or ZERO)

    def __str__(self):
        return self.number


class SalesOrderLine(BaseModel):
    order = ForeignKeyField(SalesOrder, backref="lines", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="sales_lines", on_delete="RESTRICT")
    quantity = QtyField()
    shipped_quantity = QtyField(default=ZERO)
    unit_price = MoneyField()
    discount_percent = RateField(default=ZERO)
    gst_rate = RateField(default=ZERO)
    hsn_code = CharField(max_length=10, null=True)
    line_total = MoneyField(default=ZERO)      # excluding tax
    notes = CharField(max_length=200, null=True)

    @property
    def outstanding(self):
        return (self.quantity or ZERO) - (self.shipped_quantity or ZERO)


class Fulfillment(TimestampedModel):
    """Goods physically leaving. Captures COGS at ship time."""
    number = CharField(max_length=40, unique=True)
    order = ForeignKeyField(SalesOrder, backref="fulfillments", on_delete="CASCADE")
    warehouse = ForeignKeyField(Warehouse, backref="fulfillments", on_delete="RESTRICT")
    ship_date = DateField(default=datetime.date.today)
    truck_hsrp = CharField(max_length=20, null=True)
    shipped_by = ForeignKeyField(User, backref="fulfillments", null=True,
                                 on_delete="SET NULL")
    notes = TextField(null=True)

    def __str__(self):
        return self.number


class FulfillmentLine(BaseModel):
    fulfillment = ForeignKeyField(Fulfillment, backref="lines", on_delete="CASCADE")
    order_line = ForeignKeyField(SalesOrderLine, backref="fulfillment_lines", null=True,
                                 on_delete="SET NULL")
    item = ForeignKeyField(Item, backref="fulfillment_lines", on_delete="RESTRICT")
    quantity = QtyField()
    unit_price = MoneyField(default=ZERO)
    unit_cost = CostField(default=ZERO)   # frozen COGS — never recalculated later
    lot = ForeignKeyField(Lot, backref="fulfillment_lines", null=True,
                          on_delete="SET NULL")


class Invoice(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    order = ForeignKeyField(SalesOrder, backref="invoices", on_delete="RESTRICT")
    customer = ForeignKeyField(Customer, backref="invoices", on_delete="RESTRICT")
    invoice_date = DateField(default=datetime.date.today)
    due_date = DateField(null=True)
    place_of_supply = CharField(max_length=100, null=True)
    is_interstate = BooleanField(default=False)
    subtotal = MoneyField(default=ZERO)
    cgst = MoneyField(default=ZERO)
    sgst = MoneyField(default=ZERO)
    igst = MoneyField(default=ZERO)
    round_off = MoneyField(default=ZERO)
    total = MoneyField(default=ZERO)
    created_by = ForeignKeyField(User, backref="invoices", null=True,
                                 on_delete="SET NULL")

    def __str__(self):
        return self.number


class Payment(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    order = ForeignKeyField(SalesOrder, backref="payments", on_delete="CASCADE")
    customer = ForeignKeyField(Customer, backref="payments", on_delete="RESTRICT")
    amount = MoneyField()
    payment_date = DateField(default=datetime.date.today)
    method = CharField(max_length=30, default="CASH")  # CASH/UPI/NEFT/CHEQUE/CARD
    reference = CharField(max_length=80, null=True)
    notes = CharField(max_length=200, null=True)
    recorded_by = ForeignKeyField(User, backref="payments", null=True,
                                  on_delete="SET NULL")


class CustomerReturn(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    customer = ForeignKeyField(Customer, backref="returns", on_delete="RESTRICT")
    warehouse = ForeignKeyField(Warehouse, backref="customer_returns",
                                on_delete="RESTRICT")
    order = ForeignKeyField(SalesOrder, backref="returns", null=True,
                            on_delete="SET NULL")
    return_date = DateField(default=datetime.date.today)
    reason = CharField(max_length=200, null=True)
    restock = BooleanField(default=True)   # false → returned goods are scrapped
    total = MoneyField(default=ZERO)
    created_by = ForeignKeyField(User, backref="customer_returns", null=True,
                                 on_delete="SET NULL")
    notes = TextField(null=True)


class CustomerReturnLine(BaseModel):
    customer_return = ForeignKeyField(CustomerReturn, backref="lines",
                                      on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="customer_return_lines", on_delete="RESTRICT")
    quantity = QtyField()
    unit_price = MoneyField(default=ZERO)
    unit_cost = MoneyField(default=ZERO)
    lot = ForeignKeyField(Lot, backref="customer_return_lines", null=True,
                          on_delete="SET NULL")


# --- Warehouse operations -----------------------------------------------------------

class StockAdjustment(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    warehouse = ForeignKeyField(Warehouse, backref="adjustments", on_delete="RESTRICT")
    adjustment_date = DateField(default=datetime.date.today)
    reason = CharField(max_length=30, default=AdjustmentReason.OTHER)
    notes = TextField(null=True)
    created_by = ForeignKeyField(User, backref="adjustments", null=True,
                                 on_delete="SET NULL")
    approved_by = ForeignKeyField(User, backref="approved_adjustments", null=True,
                                  on_delete="SET NULL")
    approved_at = DateTimeField(null=True)


class StockAdjustmentLine(BaseModel):
    adjustment = ForeignKeyField(StockAdjustment, backref="lines", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="adjustment_lines", on_delete="RESTRICT")
    quantity_delta = QtyField()     # signed: negative writes stock off
    unit_cost = MoneyField(default=ZERO)
    lot = ForeignKeyField(Lot, backref="adjustment_lines", null=True,
                          on_delete="SET NULL")
    note = CharField(max_length=200, null=True)


class StockTransfer(TimestampedModel):
    """Moves stock between warehouses, held in transit until received."""
    number = CharField(max_length=40, unique=True)
    from_warehouse = ForeignKeyField(Warehouse, backref="transfers_out",
                                     on_delete="RESTRICT")
    to_warehouse = ForeignKeyField(Warehouse, backref="transfers_in",
                                   on_delete="RESTRICT")
    status = CharField(max_length=20, default=TransferStatus.DRAFT)
    dispatch_date = DateField(null=True)
    received_date = DateField(null=True)
    truck_hsrp = CharField(max_length=20, null=True)
    created_by = ForeignKeyField(User, backref="transfers", null=True,
                                 on_delete="SET NULL")
    notes = TextField(null=True)


class StockTransferLine(BaseModel):
    transfer = ForeignKeyField(StockTransfer, backref="lines", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="transfer_lines", on_delete="RESTRICT")
    quantity_sent = QtyField()
    quantity_received = QtyField(default=ZERO)
    lot = ForeignKeyField(Lot, backref="transfer_lines", null=True, on_delete="SET NULL")


class CycleCount(TimestampedModel):
    number = CharField(max_length=40, unique=True)
    warehouse = ForeignKeyField(Warehouse, backref="cycle_counts", on_delete="RESTRICT")
    status = CharField(max_length=20, default=CountStatus.OPEN)
    count_date = DateField(default=datetime.date.today)
    counted_by = ForeignKeyField(User, backref="cycle_counts", null=True,
                                 on_delete="SET NULL")
    approved_by = ForeignKeyField(User, backref="approved_counts", null=True,
                                  on_delete="SET NULL")
    approved_at = DateTimeField(null=True)
    notes = TextField(null=True)


class CycleCountLine(BaseModel):
    count = ForeignKeyField(CycleCount, backref="lines", on_delete="CASCADE")
    item = ForeignKeyField(Item, backref="count_lines", on_delete="RESTRICT")
    expected_quantity = QtyField(default=ZERO)   # snapshot when the count opened
    counted_quantity = QtyField(null=True)
    lot = ForeignKeyField(Lot, backref="count_lines", null=True, on_delete="SET NULL")

    @property
    def variance(self):
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - (self.expected_quantity or ZERO)


# --- Activity -----------------------------------------------------------------------

class StockMovement(BaseModel):
    """Immutable trail of every quantity change. Powers the Recent Logs screen."""
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)
    item = ForeignKeyField(Item, backref="movements", on_delete="CASCADE")
    warehouse = ForeignKeyField(Warehouse, backref="movements", on_delete="CASCADE")
    direction = CharField(max_length=3)          # IN / OUT
    quantity = QtyField()                        # always positive; direction carries sign
    balance_after = QtyField(default=ZERO)
    unit_cost = CostField(default=ZERO)          # cost basis at the moment of the move

    doc_type = CharField(max_length=25, index=True)
    doc_number = CharField(max_length=40, null=True)
    doc_id = IntegerField(null=True)

    lot = ForeignKeyField(Lot, backref="movements", null=True, on_delete="SET NULL")
    user = ForeignKeyField(User, backref="movements", null=True, on_delete="SET NULL")
    employee = ForeignKeyField(Employee, backref="movements", null=True,
                               on_delete="SET NULL")
    supplier = ForeignKeyField(Supplier, backref="movements", null=True,
                               on_delete="SET NULL")
    customer = ForeignKeyField(Customer, backref="movements", null=True,
                               on_delete="SET NULL")
    truck_hsrp = CharField(max_length=20, null=True)
    notes = CharField(max_length=250, null=True)


class TimeEntry(BaseModel):
    """Clock in/out, normally written by a badge scan."""
    employee = ForeignKeyField(Employee, backref="time_entries", on_delete="CASCADE")
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)
    entry_type = CharField(max_length=3)          # IN / OUT
    warehouse = ForeignKeyField(Warehouse, backref="time_entries", null=True,
                                on_delete="SET NULL")
    method = CharField(max_length=10, default="SCAN")   # SCAN / MANUAL
    recorded_by = ForeignKeyField(User, backref="time_entries", null=True,
                                  on_delete="SET NULL")
    note = CharField(max_length=200, null=True)


class Task(TimestampedModel):
    """Work assigned to an employee; the Home screen shows each person's current one."""
    title = CharField(max_length=200)
    details = TextField(null=True)
    assigned_to = ForeignKeyField(Employee, backref="tasks", null=True,
                                  on_delete="CASCADE")
    assigned_by = ForeignKeyField(User, backref="assigned_tasks", null=True,
                                  on_delete="SET NULL")
    warehouse = ForeignKeyField(Warehouse, backref="tasks", null=True,
                                on_delete="SET NULL")
    status = CharField(max_length=15, default=TaskStatus.PENDING, index=True)
    priority = IntegerField(default=2)     # 1 high, 2 normal, 3 low
    due_date = DateField(null=True)
    started_at = DateTimeField(null=True)
    completed_at = DateTimeField(null=True)
    ref_doc_type = CharField(max_length=25, null=True)
    ref_doc_id = IntegerField(null=True)


class Reminder(TimestampedModel):
    """Manual notes plus system-generated alerts, shared across the team."""
    due_date = DateField(default=datetime.date.today, index=True)
    title = CharField(max_length=200)
    details = TextField(null=True)
    source = CharField(max_length=20, default=ReminderSource.MANUAL)
    ref_type = CharField(max_length=25, null=True)
    ref_id = IntegerField(null=True)
    is_done = BooleanField(default=False)
    created_by = ForeignKeyField(User, backref="reminders", null=True,
                                 on_delete="SET NULL")

    class Meta:
        # Stops the auto-alert generator creating a duplicate every time it runs.
        indexes = ((("source", "ref_type", "ref_id", "is_done"), False),)


class AuditLog(BaseModel):
    """Who changed what. Written for edits, deletions and logins."""
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)
    user = ForeignKeyField(User, backref="audit_entries", null=True,
                           on_delete="SET NULL")
    username = CharField(max_length=60, null=True)   # kept if the user is later deleted
    action = CharField(max_length=30)                # CREATE/UPDATE/DELETE/LOGIN/...
    entity = CharField(max_length=60, null=True)
    entity_id = IntegerField(null=True)
    summary = CharField(max_length=300, null=True)
    detail = TextField(null=True)


# --- Registry -----------------------------------------------------------------------

ALL_MODELS = [
    CompanySettings, NumberSequence,
    Warehouse, Employee, User,
    Category, Uom, Supplier, PriceList, Customer, Item, PriceListItem,
    StockLevel, Lot, LotStock, Serial,
    PurchaseOrder, PurchaseOrderLine, GoodsReceipt, GoodsReceiptLine,
    SupplierReturn, SupplierReturnLine,
    SalesOrder, SalesOrderLine, Fulfillment, FulfillmentLine, Invoice, Payment,
    CustomerReturn, CustomerReturnLine,
    StockAdjustment, StockAdjustmentLine, StockTransfer, StockTransferLine,
    CycleCount, CycleCountLine,
    StockMovement, TimeEntry, Task, Reminder, AuditLog,
]
