"""Settings screen — company details and all master data, behind the header gear."""
import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTabWidget, QVBoxLayout, QWidget

from database.models import (
    Category,
    CompanySettings,
    Customer,
    Employee,
    Item,
    PriceList,
    PurchaseOrder,
    Role,
    SalesOrder,
    StockLevel,
    Supplier,
    Uom,
    User,
    Warehouse,
)
from services import auth, gst, numbering
from ui import theme
from ui.dialogs.record_dialog import (
    Field,
    FormSpec,
    RecordDialog,
    email_validator,
    gstin_validator,
    make_unique_validator,
    pincode_validator,
)
from ui.screens.masters import MasterListTab
from ui.widgets import common
from ui.widgets.table import CENTER, Column, RIGHT, money_cell, qty_cell, text_cell


def _active(model):
    return lambda: list(model.select().order_by(model.name))


def _yes_no(value):
    return text_cell("Yes" if value else "No", align=CENTER,
                     tone=theme.SUCCESS if value else theme.TEXT_MUTED)


def _apply_state(values):
    """The state combo stores a GST code; keep the readable name in step."""
    code = values.get("state_code")
    values["state"] = gst.state_name(code) if code else None
    return values


class SettingsScreen(QWidget):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(common.screen_title("SETTINGS"))
        header.addStretch()
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(CompanyTab(user, self), "Company")
        self.tabs.addTab(self._warehouses_tab(), "Warehouses")
        self.tabs.addTab(self._categories_tab(), "Categories")
        self.tabs.addTab(self._suppliers_tab(), "Suppliers")
        self.tabs.addTab(self._customers_tab(), "Customers")
        self.tabs.addTab(self._price_lists_tab(), "Price Lists")
        self.tabs.addTab(self._employees_tab(), "Employees")
        if auth.can(user, auth.PERM_MANAGE_USERS):
            self.tabs.addTab(UsersTab(user, self), "Users and Roles")
        self.tabs.addTab(self._uoms_tab(), "Units")
        layout.addWidget(self.tabs, 1)

    def refresh(self):
        widget = self.tabs.currentWidget()
        if hasattr(widget, "refresh"):
            widget.refresh()

    # --- Warehouses -----------------------------------------------------------------

    def _warehouses_tab(self):
        def spec(instance):
            return FormSpec("Warehouse", [
                Field("code", "Code", required=True, uppercase=True, max_length=20,
                      placeholder="e.g. WH1",
                      validator=make_unique_validator(Warehouse, Warehouse.code,
                                                      instance, "code")),
                Field("name", "Name", required=True, placeholder="e.g. Pune Depot"),
                Field("state_code", "State", kind="state",
                      help="Determines whether GST on an order is split CGST+SGST "
                           "(same state) or charged as IGST (different state)."),
                Field("address_line1", "Address"),
                Field("city", "City"),
                Field("pincode", "PIN code", max_length=10,
                      validator=pincode_validator),
                Field("phone", "Phone"),
                Field("is_active", "Active — available for new stock and orders",
                      kind="check", default=True),
            ])

        def save(values, instance):
            values = _apply_state(values)
            if instance is None:
                record = Warehouse.create(**values)
                action = "CREATE"
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
                action = "UPDATE"
            auth.record_audit(self.user, action, "Warehouse", record.id,
                              f"{action.title()}d warehouse {record.name}")
            return record

        def row(w):
            stocked = (StockLevel.select()
                       .where((StockLevel.warehouse == w) & (StockLevel.on_hand > 0))
                       .count())
            return [
                text_cell(w.code, bold=True),
                text_cell(w.name),
                text_cell(gst.state_name(w.state_code) or "—"),
                text_cell(w.city or "—"),
                text_cell(str(stocked), align=RIGHT),
                _yes_no(w.is_active),
            ]

        def guard(w):
            if StockLevel.select().where(
                (StockLevel.warehouse == w) & (StockLevel.on_hand > 0)
            ).exists():
                return ("This warehouse still holds stock. Transfer or write it off "
                        "first, or mark the warehouse inactive.")
            if (PurchaseOrder.select().where(PurchaseOrder.warehouse == w).exists()
                    or SalesOrder.select().where(SalesOrder.warehouse == w).exists()):
                return ("Orders reference this warehouse. Mark it inactive instead so "
                        "the order history stays intact.")
            return None

        return MasterListTab(
            self, self.user,
            title="Warehouses", singular="Warehouse",
            columns=[
                Column("Code", width=110),
                Column("Name", width="stretch"),
                Column("State", width="content"),
                Column("City", width="content"),
                Column("Items Stocked", width=130, align=RIGHT),
                Column("Active", width=90, align=CENTER),
            ],
            query_fn=_active(Warehouse), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_SETTINGS,
            delete_guard=guard,
            search_placeholder="Search warehouses…",
            intro="Stock is tracked separately in each warehouse. A warehouse's state "
                  "drives the GST split on its orders.",
        )

    # --- Categories -----------------------------------------------------------------

    def _categories_tab(self):
        def spec(instance):
            return FormSpec("Category", [
                Field("name", "Name", required=True,
                      validator=make_unique_validator(Category, Category.name,
                                                      instance, "name")),
                Field("parent", "Parent category", kind="combo",
                      choices=lambda: [c for c in Category.select().order_by(Category.name)
                                       if instance is None or c.id != instance.id],
                      label_fn=lambda c: c.name,
                      help="Optional — lets you group categories into a hierarchy."),
                Field("description", "Description", kind="textarea"),
                Field("is_active", "Active", kind="check", default=True),
            ], height=440)

        def save(values, instance):
            if instance is None:
                record = Category.create(**values)
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
            return record

        def row(c):
            count = Item.select().where(Item.category == c).count()
            return [
                text_cell(c.name, bold=True),
                text_cell(c.parent.name if c.parent else "—"),
                text_cell(str(count), align=RIGHT),
                _yes_no(c.is_active),
            ]

        def guard(c):
            count = Item.select().where(Item.category == c).count()
            if count:
                return (f"{count} item(s) are in this category. Move them to another "
                        f"category first.")
            return None

        return MasterListTab(
            self, self.user,
            title="Categories", singular="Category",
            columns=[
                Column("Name", width="stretch"),
                Column("Parent", width="content"),
                Column("Items", width=90, align=RIGHT),
                Column("Active", width=90, align=CENTER),
            ],
            query_fn=_active(Category), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_ITEMS, delete_guard=guard,
            search_placeholder="Search categories…",
        )

    # --- Suppliers ------------------------------------------------------------------

    def _suppliers_tab(self):
        def spec(instance):
            return FormSpec("Supplier", [
                Field("code", "Code", required=True, uppercase=True, max_length=30,
                      default=None if instance else numbering.peek_number("SUPPLIER").replace("/", "-"),
                      validator=make_unique_validator(Supplier, Supplier.code,
                                                      instance, "code")),
                Field("name", "Business name", required=True),
                Field("gstin", "GSTIN", uppercase=True, max_length=15,
                      placeholder="27AAACS1234A1Z5", validator=gstin_validator),
                Field("state_code", "State", kind="state",
                      help="Used to decide CGST+SGST vs IGST on purchase orders."),
                Field("contact_person", "Contact person", section="Contact"),
                Field("phone", "Phone"),
                Field("email", "Email", validator=email_validator),
                Field("address_line1", "Address line 1"),
                Field("address_line2", "Address line 2"),
                Field("city", "City"),
                Field("pincode", "PIN code", max_length=10, validator=pincode_validator),
                Field("lead_time_days", "Lead time (days)", kind="int", default=7,
                      maximum=365, section="Terms",
                      help="How long they normally take to deliver. Sets the expected "
                           "date on new purchase orders and flags overdue deliveries."),
                Field("payment_terms_days", "Payment terms (days)", kind="int",
                      default=30, maximum=365),
                Field("notes", "Notes", kind="textarea"),
                Field("is_active", "Active", kind="check", default=True),
            ], height=700)

        def save(values, instance):
            values = _apply_state(values)
            if not values.get("code"):
                values["code"] = numbering.next_number("SUPPLIER").replace("/", "-")
            if instance is None:
                record = Supplier.create(**values)
                action = "CREATE"
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
                action = "UPDATE"
            auth.record_audit(self.user, action, "Supplier", record.id,
                              f"{action.title()}d supplier {record.name}")
            return record

        def row(s):
            open_orders = PurchaseOrder.select().where(
                PurchaseOrder.supplier == s
            ).count()
            return [
                text_cell(s.code, bold=True),
                text_cell(s.name),
                text_cell(s.gstin or "—"),
                text_cell(gst.state_name(s.state_code) or "—"),
                text_cell(s.phone or "—"),
                qty_cell(s.lead_time_days, suffix=" d"),
                text_cell(str(open_orders), align=RIGHT),
                _yes_no(s.is_active),
            ]

        def guard(s):
            if PurchaseOrder.select().where(PurchaseOrder.supplier == s).exists():
                return ("This supplier has purchase orders on record. Mark them inactive "
                        "instead so the history is preserved.")
            return None

        return MasterListTab(
            self, self.user,
            title="Suppliers", singular="Supplier",
            columns=[
                Column("Code", width=110),
                Column("Name", width="stretch"),
                Column("GSTIN", width=150),
                Column("State", width="content"),
                Column("Phone", width="content"),
                Column("Lead Time", width=100, align=RIGHT),
                Column("Orders", width=80, align=RIGHT),
                Column("Active", width=80, align=CENTER),
            ],
            query_fn=_active(Supplier), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_PARTNERS, delete_guard=guard,
            search_placeholder="Search suppliers…",
        )

    # --- Customers ------------------------------------------------------------------

    def _customers_tab(self):
        def spec(instance):
            return FormSpec("Customer", [
                Field("code", "Code", required=True, uppercase=True, max_length=30,
                      default=None if instance else numbering.peek_number("CUSTOMER").replace("/", "-"),
                      validator=make_unique_validator(Customer, Customer.code,
                                                      instance, "code")),
                Field("name", "Business name", required=True),
                Field("gstin", "GSTIN", uppercase=True, max_length=15,
                      placeholder="29AAACS1234A1Z5", validator=gstin_validator),
                Field("state_code", "State", kind="state",
                      help="Place of supply — decides CGST+SGST vs IGST on their invoices."),
                Field("contact_person", "Contact person", section="Contact"),
                Field("phone", "Phone"),
                Field("email", "Email", validator=email_validator),
                Field("billing_address", "Billing address", kind="textarea"),
                Field("delivery_address", "Delivery address", kind="textarea"),
                Field("delivery_pincode", "Delivery PIN code", max_length=10,
                      validator=pincode_validator),
                Field("price_list", "Price list", kind="combo", section="Terms",
                      choices=lambda: list(PriceList.select().where(
                          PriceList.is_active == True).order_by(PriceList.name)),  # noqa: E712
                      label_fn=lambda p: p.name,
                      help="Prices from this list are applied automatically on their orders."),
                Field("payment_terms_days", "Payment terms (days)", kind="int",
                      default=30, maximum=365),
                Field("credit_limit", "Credit limit", kind="money"),
                Field("notes", "Notes", kind="textarea"),
                Field("is_active", "Active", kind="check", default=True),
            ], height=720)

        def save(values, instance):
            values = _apply_state(values)
            if not values.get("code"):
                values["code"] = numbering.next_number("CUSTOMER").replace("/", "-")
            if instance is None:
                record = Customer.create(**values)
                action = "CREATE"
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
                action = "UPDATE"
            auth.record_audit(self.user, action, "Customer", record.id,
                              f"{action.title()}d customer {record.name}")
            return record

        def row(c):
            return [
                text_cell(c.code, bold=True),
                text_cell(c.name),
                text_cell(c.gstin or "—"),
                text_cell(gst.state_name(c.state_code) or "—"),
                text_cell(c.delivery_pincode or "—", align=CENTER),
                text_cell(c.price_list.name if c.price_list else "— default —"),
                money_cell(c.credit_limit),
                _yes_no(c.is_active),
            ]

        def guard(c):
            if SalesOrder.select().where(SalesOrder.customer == c).exists():
                return ("This customer has orders on record. Mark them inactive instead "
                        "so the history is preserved.")
            return None

        return MasterListTab(
            self, self.user,
            title="Customers", singular="Customer",
            columns=[
                Column("Code", width=110),
                Column("Name", width="stretch"),
                Column("GSTIN", width=150),
                Column("State", width="content"),
                Column("Delivery PIN", width=110, align=CENTER),
                Column("Price List", width="content"),
                Column("Credit Limit", width=120, align=RIGHT),
                Column("Active", width=80, align=CENTER),
            ],
            query_fn=_active(Customer), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_PARTNERS, delete_guard=guard,
            search_placeholder="Search customers…",
        )

    # --- Price lists ----------------------------------------------------------------

    def _price_lists_tab(self):
        def spec(instance):
            return FormSpec("Price List", [
                Field("name", "Name", required=True,
                      placeholder="e.g. Wholesale",
                      validator=make_unique_validator(PriceList, PriceList.name,
                                                      instance, "name")),
                Field("description", "Description"),
                Field("discount_percent", "Default discount", kind="percent",
                      help="Applied to the item's list price when no specific price is "
                           "set for that item on this list."),
                Field("is_default", "Default list for new customers", kind="check"),
                Field("is_active", "Active", kind="check", default=True),
            ], height=460)

        def save(values, instance):
            if values.get("is_default"):
                PriceList.update(is_default=False).execute()
            if instance is None:
                record = PriceList.create(**values)
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
            return record

        def row(p):
            customers = Customer.select().where(Customer.price_list == p).count()
            return [
                text_cell(p.name, bold=True),
                text_cell(p.description or "—"),
                text_cell(theme.percent(p.discount_percent), align=RIGHT),
                text_cell(str(customers), align=RIGHT),
                _yes_no(p.is_default),
                _yes_no(p.is_active),
            ]

        def guard(p):
            count = Customer.select().where(Customer.price_list == p).count()
            if count:
                return f"{count} customer(s) use this price list. Reassign them first."
            return None

        return MasterListTab(
            self, self.user,
            title="Price Lists", singular="Price List",
            columns=[
                Column("Name", width=180),
                Column("Description", width="stretch"),
                Column("Default Discount", width=140, align=RIGHT),
                Column("Customers", width=100, align=RIGHT),
                Column("Default", width=90, align=CENTER),
                Column("Active", width=80, align=CENTER),
            ],
            query_fn=_active(PriceList), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_PRICING,
            delete_guard=guard,
            search_placeholder="Search price lists…",
            intro="Customer pricing tiers. Per-item prices are set from the item's own "
                  "screen; the default discount below is the fallback.",
        )

    # --- Employees ------------------------------------------------------------------

    def _employees_tab(self):
        def spec(instance):
            return FormSpec("Employee", [
                Field("code", "Badge code", required=True, uppercase=True, max_length=30,
                      default=None if instance else numbering.peek_number("EMPLOYEE").replace("/", "-"),
                      validator=make_unique_validator(Employee, Employee.code,
                                                      instance, "badge code"),
                      help="Printed as the barcode on their ID badge. Scanning it clocks "
                           "them in and out."),
                Field("name", "Full name", required=True),
                Field("designation", "Designation",
                      placeholder="e.g. Picker, Driver, Supervisor"),
                Field("warehouse", "Warehouse", kind="combo",
                      choices=lambda: list(Warehouse.select().where(
                          Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
                      label_fn=lambda w: w.name),
                Field("phone", "Phone"),
                Field("email", "Email", validator=email_validator),
                Field("joined_on", "Joined on", kind="date"),
                Field("is_active", "Active — appears on the Home screen roster",
                      kind="check", default=True),
            ], height=560)

        def save(values, instance):
            if not values.get("code"):
                values["code"] = numbering.next_number("EMPLOYEE").replace("/", "-")
            if instance is None:
                record = Employee.create(**values)
            else:
                for key, value in values.items():
                    setattr(instance, key, value)
                instance.save()
                record = instance
            return record

        def row(e):
            return [
                text_cell(e.code, bold=True),
                text_cell(e.name),
                text_cell(e.designation or "—"),
                text_cell(e.warehouse.name if e.warehouse else "—"),
                text_cell(e.phone or "—"),
                text_cell(theme.date_short(e.joined_on), align=CENTER),
                _yes_no(e.is_active),
            ]

        return MasterListTab(
            self, self.user,
            title="Employees", singular="Employee",
            columns=[
                Column("Badge", width=120),
                Column("Name", width="stretch"),
                Column("Designation", width="content"),
                Column("Warehouse", width="content"),
                Column("Phone", width="content"),
                Column("Joined", width=120, align=CENTER),
                Column("Active", width=80, align=CENTER),
            ],
            query_fn=_active(Employee), row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_EMPLOYEES,
            search_placeholder="Search employees…",
            intro="People working in the warehouse. A login is only needed for staff who "
                  "use the system — create those under Users & Roles.",
        )

    # --- Units ----------------------------------------------------------------------

    def _uoms_tab(self):
        def spec(instance):
            return FormSpec("Unit", [
                Field("code", "Code", required=True, uppercase=True, max_length=15,
                      placeholder="e.g. KG",
                      validator=make_unique_validator(Uom, Uom.code, instance, "code")),
                Field("name", "Name", required=True, placeholder="e.g. Kilogram"),
                Field("allow_decimal", "Allows fractional quantities", kind="check",
                      help="Tick for weights and volumes. Leave unticked for discrete "
                           "units so nobody can order half a carton."),
            ], height=400)

        def save(values, instance):
            if instance is None:
                return Uom.create(**values)
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            return instance

        def row(u):
            count = Item.select().where(Item.base_uom == u).count()
            return [
                text_cell(u.code, bold=True),
                text_cell(u.name),
                _yes_no(u.allow_decimal),
                text_cell(str(count), align=RIGHT),
            ]

        def guard(u):
            count = Item.select().where(
                (Item.base_uom == u) | (Item.purchase_uom == u)
            ).count()
            if count:
                return f"{count} item(s) use this unit."
            return None

        return MasterListTab(
            self, self.user,
            title="Units", singular="Unit",
            columns=[
                Column("Code", width=120),
                Column("Name", width="stretch"),
                Column("Fractional", width=120, align=CENTER),
                Column("Items", width=90, align=RIGHT),
            ],
            query_fn=lambda: list(Uom.select().order_by(Uom.code)),
            row_fn=row, spec_fn=spec, save_fn=save,
            permission=auth.PERM_MANAGE_ITEMS, delete_guard=guard,
            search_placeholder="Search units…",
        )


class CompanyTab(QWidget):
    """Company identity and GST details, used as the header on every invoice."""

    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = user

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        layout.addWidget(common.subtle(
            "These details print on every tax invoice, delivery challan and purchase "
            "order. GST invoices are not valid without a correct legal name, address "
            "and GSTIN."
        ))

        self.card = common.Card(padding=20)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        self.card.add(self.summary)
        layout.addWidget(self.card)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.edit_button = common.action_button("Edit company details", self.edit,
                                                "primary")
        self.edit_button.setEnabled(auth.can(user, auth.PERM_MANAGE_SETTINGS))
        if not self.edit_button.isEnabled():
            self.edit_button.setToolTip("Only an Owner can change company details.")
        buttons.addWidget(self.edit_button)
        layout.addLayout(buttons)
        layout.addStretch()

        self.refresh()

    def _settings(self):
        record = CompanySettings.get_or_none()
        if record is None:
            record = CompanySettings.create(legal_name="My Company")
        return record

    def refresh(self):
        s = self._settings()
        missing = []
        if not s.gstin:
            missing.append("GSTIN")
        if not s.address_line1:
            missing.append("address")
        if not s.state_code:
            missing.append("state")
        if s.legal_name in (None, "", "My Company"):
            missing.append("legal name")

        warning = ""
        if missing:
            warning = (
                f"<p style='color:{theme.DANGER};'><b>Incomplete:</b> "
                f"{', '.join(missing)} still needed before invoices are GST-valid.</p>"
            )

        self.summary.setText(f"""
            <div style='font-size:15px; line-height:1.7;'>
              <b style='font-size:19px;'>{s.legal_name or '—'}</b><br>
              {('<span style="color:%s;">%s</span><br>' % (theme.TEXT_MUTED, s.trade_name)) if s.trade_name else ''}
              <b>GSTIN:</b> {s.gstin or '—'} &nbsp;&nbsp; <b>PAN:</b> {s.pan or '—'}<br>
              <b>Address:</b> {', '.join(filter(None, [s.address_line1, s.address_line2, s.city, s.pincode])) or '—'}<br>
              <b>State:</b> {gst.state_name(s.state_code) or '—'}<br>
              <b>Phone:</b> {s.phone or '—'} &nbsp;&nbsp; <b>Email:</b> {s.email or '—'}<br>
              <b>Bank:</b> {s.bank_name or '—'} &nbsp;&nbsp; <b>A/C:</b> {s.bank_account or '—'}
                &nbsp;&nbsp; <b>IFSC:</b> {s.bank_ifsc or '—'}<br>
              <b>Financial year starts:</b> {datetime.date(2000, s.fy_start_month, 1).strftime('%B')}
                &nbsp;&nbsp; <b>Current FY:</b> {numbering.financial_year_label()}
            </div>
            {warning}
        """)

    def edit(self):
        record = self._settings()
        spec = FormSpec("Company Details", [
            Field("legal_name", "Legal name", required=True,
                  help="Exactly as registered — this is what appears on tax invoices."),
            Field("trade_name", "Trade name"),
            Field("gstin", "GSTIN", uppercase=True, max_length=15,
                  validator=gstin_validator),
            Field("pan", "PAN", uppercase=True, max_length=10),
            Field("address_line1", "Address line 1", section="Registered address"),
            Field("address_line2", "Address line 2"),
            Field("city", "City"),
            Field("state_code", "State", kind="state"),
            Field("pincode", "PIN code", max_length=10, validator=pincode_validator),
            Field("phone", "Phone", section="Contact"),
            Field("email", "Email", validator=email_validator),
            Field("bank_name", "Bank name", section="Bank details for invoices"),
            Field("bank_account", "Account number"),
            Field("bank_ifsc", "IFSC code", uppercase=True, max_length=20),
            Field("fy_start_month", "Financial year starts", kind="combo",
                  allow_blank=False, section="Accounting",
                  choices=lambda: [(m, datetime.date(2000, m, 1).strftime("%B"))
                                   for m in range(1, 13)],
                  help="India's financial year normally starts in April."),
            Field("invoice_terms", "Invoice terms and conditions", kind="textarea"),
        ], height=720)

        def save(values, instance):
            values = _apply_state(values)
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            auth.record_audit(self.user, "UPDATE", "CompanySettings", instance.id,
                              "Updated company details")
            return instance

        dialog = RecordDialog(self, spec, instance=record, save_fn=save)
        if dialog.exec():
            self.refresh()


class UsersTab(MasterListTab):
    """User accounts. Owner-only, with password set on create and reset on demand."""

    def __init__(self, user, parent=None):
        self.current_user = user
        reset_button = common.action_button("Reset password", self.reset_password,
                                            "action")
        super().__init__(
            parent, user,
            title="Users", singular="User",
            columns=[
                Column("Username", width=150),
                Column("Full Name", width="stretch"),
                Column("Role", width=120, align=CENTER),
                Column("Warehouse", width="content"),
                Column("Linked Employee", width="content"),
                Column("Last Signed In", width=170, align=CENTER),
                Column("Active", width=80, align=CENTER),
            ],
            query_fn=lambda: list(User.select().order_by(User.full_name)),
            row_fn=self._row, spec_fn=self._spec, save_fn=self._save,
            permission=auth.PERM_MANAGE_USERS,
            delete_guard=self._guard,
            search_placeholder="Search users…",
            extra_buttons=[reset_button],
            intro="Owners manage users and settings; Managers run the warehouse and see "
                  "analytics; Clerks handle day-to-day stock movements.",
        )

    def _row(self, u):
        return [
            text_cell(u.username, bold=True),
            text_cell(u.full_name),
            text_cell(Role.LABELS.get(u.role, u.role), align=CENTER,
                      tone=theme.TEAL if u.role == Role.OWNER else None),
            text_cell(u.warehouse.name if u.warehouse else "— all —"),
            text_cell(u.employee.name if u.employee else "—"),
            text_cell(theme.datetime_short(u.last_login_at), align=CENTER),
            _yes_no(u.is_active),
        ]

    def _spec(self, instance):
        fields = [
            Field("username", "Username", required=True, max_length=60,
                  validator=make_unique_validator(User, User.username, instance,
                                                  "username")),
            Field("full_name", "Full name", required=True),
            Field("role", "Role", kind="combo", allow_blank=False,
                  choices=lambda: [(r, Role.LABELS[r]) for r in Role.ALL],
                  help="Owner: everything, including users and deletion. "
                       "Manager: items, partners, pricing, payments, analytics. "
                       "Clerk: day-to-day stock movements only."),
            Field("warehouse", "Default warehouse", kind="combo",
                  choices=lambda: list(Warehouse.select().where(
                      Warehouse.is_active == True).order_by(Warehouse.name)),  # noqa: E712
                  label_fn=lambda w: w.name,
                  help="Pre-selected when they open the Inventory screen."),
            Field("employee", "Linked employee record", kind="combo",
                  choices=lambda: list(Employee.select().where(
                      Employee.is_active == True).order_by(Employee.name)),  # noqa: E712
                  label_fn=lambda e: f"{e.name} ({e.code})",
                  help="Links this login to a badge, so their movements and hours line up."),
            Field("is_active", "Active — can sign in", kind="check", default=True),
        ]
        if instance is None:
            fields.append(Field("password", "Password", kind="password", required=True,
                                section="Credentials",
                                help="At least 8 characters. They can change it later.",
                                validator=lambda v: auth.password_problem(v)))
        return FormSpec("User", fields, height=620)

    def _save(self, values, instance):
        password = values.pop("password", None)
        if instance is None:
            values["password_hash"] = auth.hash_password(password)
            record = User.create(**values)
            # On the web/multi-tenant build, claim the username on the shared login page.
            auth.sync_user_directory(record)
            auth.record_audit(self.current_user, "CREATE", "User", record.id,
                              f"Created user '{record.username}' as {record.role}")
            return record

        if (instance.id == self.current_user.id
                and (values.get("role") != Role.OWNER or not values.get("is_active"))):
            raise ValueError(
                "You cannot remove your own Owner role or deactivate your own account — "
                "that would lock you out. Ask another Owner to do it."
            )
        if instance.role == Role.OWNER and values.get("role") != Role.OWNER:
            others = (User.select()
                      .where((User.role == Role.OWNER) & (User.is_active == True)  # noqa: E712
                             & (User.id != instance.id))
                      .count())
            if others == 0:
                raise ValueError(
                    "This is the only active Owner. Promote someone else to Owner first."
                )
        for key, value in values.items():
            setattr(instance, key, value)
        instance.save()
        auth.record_audit(self.current_user, "UPDATE", "User", instance.id,
                          f"Updated user '{instance.username}'")
        return instance

    def _guard(self, u):
        if u.id == self.current_user.id:
            return "You cannot delete the account you are signed in with."
        if u.role == Role.OWNER:
            others = (User.select()
                      .where((User.role == Role.OWNER) & (User.is_active == True)  # noqa: E712
                             & (User.id != u.id))
                      .count())
            if others == 0:
                return "This is the only Owner account. Promote someone else first."
        return None

    def reset_password(self):
        record = self.table.current_payload()
        if record is None:
            common.info(self, "Select a user", "Choose a user in the table first.")
            return

        temp = auth.generate_temp_password()
        if not common.confirm(
            self, "Reset password?",
            f"Reset the password for '{record.username}'?",
            f"They will be given the temporary password below and asked to change it "
            f"when they next sign in.\n\n    {temp}\n\nMake a note of it now — it is not "
            f"shown again.",
            confirm_label="Reset password",
        ):
            return

        record.password_hash = auth.hash_password(temp)
        record.must_change_password = True
        record.failed_attempts = 0
        record.locked_until = None
        record.save()
        auth.record_audit(self.current_user, "PASSWORD_RESET", "User", record.id,
                          f"Reset password for '{record.username}'")
        common.info(self, "Password reset",
                    f"Temporary password for {record.username}:",
                    f"    {temp}\n\nGive this to them directly.")
        self.refresh()
