"""Create or edit an item, with optional opening stock for brand-new items."""
from decimal import Decimal

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from database.connection import db
from database.models import Category, Item, Supplier, Uom, Warehouse
from services import auth, gst, inventory, numbering
from ui import theme
from ui.widgets import common


class ItemDialog(QDialog):
    """`item=None` creates; otherwise edits in place."""

    def __init__(self, parent=None, item=None, user=None):
        super().__init__(parent)
        self.item = item
        self.user = user
        self.is_new = item is None
        self.saved_item = None

        self.setWindowTitle("New Item" if self.is_new else f"Edit — {item.name}")
        self.setMinimumSize(680, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        heading = QLabel("New Item" if self.is_new else "Edit Item")
        heading.setObjectName("ScreenTitle")
        layout.addWidget(heading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_details_tab(), "Details")
        self.tabs.addTab(self._build_stock_tab(), "Stock and Reordering")
        self.tabs.addTab(self._build_tracking_tab(), "Tracking")
        layout.addWidget(self.tabs, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(common.action_button("Cancel", self.reject, "action"))
        buttons.addWidget(common.action_button("Save Item", self.save, "primary"))
        layout.addLayout(buttons)

        self._load_lookups()
        if not self.is_new:
            self._populate()

    # --- Tabs -----------------------------------------------------------------------

    def _scrollable(self, inner):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(inner)
        return area

    def _build_details_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(18, 18, 18, 18)
        form.setVerticalSpacing(12)

        sku_row = QHBoxLayout()
        self.sku_input = QLineEdit()
        self.sku_input.setPlaceholderText("Unique item code")
        sku_row.addWidget(self.sku_input, 1)
        if self.is_new:
            sku_row.addWidget(common.action_button("Generate", self._generate_sku, "ghost"))

        self.barcode_input = QLineEdit()
        self.barcode_input.setPlaceholderText("Scan or type the barcode")

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Item name as staff will recognise it")

        self.description_input = QPlainTextEdit()
        self.description_input.setFixedHeight(64)

        self.category_combo = common.ComboField(allow_blank=True,
                                                blank_text="— uncategorised —")
        self.supplier_combo = common.ComboField(allow_blank=True,
                                                blank_text="— none —")

        self.hsn_input = QLineEdit()
        self.hsn_input.setPlaceholderText("e.g. 2202")
        self.hsn_input.setMaxLength(10)

        self.gst_combo = common.ComboField()
        self.gst_combo.setEditable(True)
        self.gst_combo.load_choices([(str(r), f"{r}%") for r in gst.COMMON_GST_RATES],
                                    selected="18")

        self.selling_price = common.DecimalSpin(decimals=2, prefix="₹ ")
        self.cost_display = QLabel("—")
        self.cost_display.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        self.active_check = common.checkbox("Active — appears in lists and can be ordered",
                                            checked=True)

        form.addRow(common.field_label("SKU *"), sku_row)
        form.addRow(common.field_label("Barcode"), self.barcode_input)
        form.addRow(common.field_label("Name *"), self.name_input)
        form.addRow(common.field_label("Description"), self.description_input)
        form.addRow(common.field_label("Category"), self.category_combo)
        form.addRow(common.field_label("Preferred supplier"), self.supplier_combo)
        form.addRow(common.field_label("HSN code"), self.hsn_input)
        form.addRow(common.field_label("GST rate"), self.gst_combo)
        form.addRow(common.field_label("Selling price"), self.selling_price)
        form.addRow(common.field_label("Average cost"), self.cost_display)
        form.addRow("", self.active_check)

        if not auth.can(self.user, auth.PERM_VIEW_COST):
            self.cost_display.setText("hidden for your role")
        return self._scrollable(page)

    def _build_stock_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        form = QFormLayout()
        form.setVerticalSpacing(12)

        self.base_uom_combo = common.ComboField()
        self.purchase_uom_combo = common.ComboField(allow_blank=True,
                                                    blank_text="— same as base —")
        self.pack_size = common.DecimalSpin(decimals=3)
        self.pack_size.setValue(1)

        self.min_level = common.DecimalSpin(decimals=3)
        self.max_level = common.DecimalSpin(decimals=3)

        form.addRow(common.field_label("Base unit *"), self.base_uom_combo)
        form.addRow(common.field_label("Purchase unit"), self.purchase_uom_combo)
        form.addRow(common.field_label("Base units per purchase unit"), self.pack_size)
        form.addRow(common.field_label("Default minimum level"), self.min_level)
        form.addRow(common.field_label("Default maximum level"), self.max_level)
        outer.addLayout(form)

        outer.addWidget(common.subtle(
            "Minimum and maximum are the defaults for every warehouse. You can override "
            "them per warehouse from the Inventory screen. 'Order Max' tops stock up to "
            "the maximum; low stock triggers a reminder at or below the minimum."
        ))

        if self.is_new:
            outer.addWidget(common.Divider())
            outer.addWidget(common.section_title("Opening stock (optional)"))
            outer.addWidget(common.subtle(
                "Record what you already have on the shelf. This posts a movement so the "
                "audit trail starts from a known quantity."
            ))
            opening = QFormLayout()
            opening.setVerticalSpacing(12)
            self.opening_warehouse = common.ComboField(allow_blank=True,
                                                       blank_text="— no opening stock —")
            self.opening_qty = common.DecimalSpin(decimals=3)
            self.opening_cost = common.DecimalSpin(decimals=2, prefix="₹ ")
            opening.addRow(common.field_label("Warehouse"), self.opening_warehouse)
            opening.addRow(common.field_label("Quantity"), self.opening_qty)
            opening.addRow(common.field_label("Unit cost"), self.opening_cost)
            outer.addLayout(opening)
        else:
            self.opening_warehouse = None

        outer.addStretch()
        return self._scrollable(page)

    def _build_tracking_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        self.lot_check = common.checkbox(
            "Lot / batch tracked — record a batch number and expiry on receipt"
        )
        self.serial_check = common.checkbox(
            "Serial tracked — every individual unit has a unique serial number"
        )
        self.shelf_life = common.DecimalSpin(decimals=0, maximum=100000)

        outer.addWidget(self.lot_check)
        outer.addWidget(common.subtle(
            "Lot-tracked items are picked earliest-expiry-first, and you'll get a "
            "reminder before a batch expires."
        ))
        outer.addWidget(common.Divider())
        outer.addWidget(self.serial_check)
        outer.addWidget(common.subtle(
            "Serial tracking means scanning each unit in and out. Use it for high-value "
            "goods only — it slows down receiving and picking considerably."
        ))
        outer.addWidget(common.Divider())

        shelf_form = QFormLayout()
        shelf_form.addRow(common.field_label("Shelf life (days)"), self.shelf_life)
        outer.addLayout(shelf_form)
        outer.addWidget(common.subtle(
            "If set, expiry dates are calculated automatically when a batch arrives."
        ))

        self.lot_check.toggled.connect(
            lambda on: self.shelf_life.setEnabled(on)
        )
        self.shelf_life.setEnabled(False)
        outer.addStretch()
        return self._scrollable(page)

    # --- Data -----------------------------------------------------------------------

    def _load_lookups(self):
        self.category_combo.load(
            Category.select().where(Category.is_active == True).order_by(Category.name),  # noqa: E712
            label=lambda c: c.name,
        )
        self.supplier_combo.load(
            Supplier.select().where(Supplier.is_active == True).order_by(Supplier.name),  # noqa: E712
            label=lambda s: f"{s.name} ({s.code})",
        )
        uoms = list(Uom.select().order_by(Uom.code))
        self.base_uom_combo.load(uoms, label=lambda u: f"{u.code} — {u.name}")
        self.purchase_uom_combo.load(uoms, label=lambda u: f"{u.code} — {u.name}")
        if self.opening_warehouse is not None:
            self.opening_warehouse.load(
                Warehouse.select().where(Warehouse.is_active == True).order_by(Warehouse.name),  # noqa: E712
                label=lambda w: f"{w.name} ({w.code})",
            )

    def _generate_sku(self):
        self.sku_input.setText(numbering.next_number("ITEM").replace("/", "-"))

    def _populate(self):
        item = self.item
        self.sku_input.setText(item.sku)
        self.barcode_input.setText(item.barcode or "")
        self.name_input.setText(item.name)
        self.description_input.setPlainText(item.description or "")
        if item.category:
            self.category_combo.select_record(item.category)
        if item.preferred_supplier:
            self.supplier_combo.select_record(item.preferred_supplier)
        self.hsn_input.setText(item.hsn_code or "")
        self.gst_combo.setCurrentText(f"{theme.quantity(item.gst_rate, 2)}%")
        self.selling_price.setValue(float(item.selling_price or 0))
        if auth.can(self.user, auth.PERM_VIEW_COST):
            self.cost_display.setText(
                f"{theme.money(item.avg_cost)}  (moving average, updated on each receipt)"
            )
        self.active_check.setChecked(bool(item.is_active))

        if item.base_uom:
            self.base_uom_combo.select_record(item.base_uom)
        if item.purchase_uom:
            self.purchase_uom_combo.select_record(item.purchase_uom)
        self.pack_size.setValue(float(item.pack_size or 1))
        self.min_level.setValue(float(item.default_min_level or 0))
        self.max_level.setValue(float(item.default_max_level or 0))

        self.lot_check.setChecked(bool(item.is_lot_tracked))
        self.serial_check.setChecked(bool(item.is_serial_tracked))
        self.shelf_life.setEnabled(bool(item.is_lot_tracked))
        self.shelf_life.setValue(float(item.shelf_life_days or 0))

    # --- Save -----------------------------------------------------------------------

    def _fail(self, message, tab_index=None, widget=None):
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        if tab_index is not None:
            self.tabs.setCurrentIndex(tab_index)
        if widget is not None:
            widget.setFocus()
        return False

    def _parse_gst_rate(self):
        raw = self.gst_combo.currentText().strip().rstrip("%").strip()
        try:
            rate = Decimal(raw or "0")
        except Exception:
            return None
        if rate < 0 or rate > 100:
            return None
        return rate

    def save(self):
        self.error_label.setVisible(False)

        sku = self.sku_input.text().strip().upper()
        name = self.name_input.text().strip()
        if not sku:
            return self._fail("SKU is required.", 0, self.sku_input)
        if not name:
            return self._fail("Item name is required.", 0, self.name_input)

        clash = Item.get_or_none(Item.sku == sku)
        if clash is not None and (self.is_new or clash.id != self.item.id):
            return self._fail(f"SKU '{sku}' is already used by {clash.name}.", 0,
                              self.sku_input)

        barcode = self.barcode_input.text().strip() or None
        if barcode:
            clash = Item.get_or_none(Item.barcode == barcode)
            if clash is not None and (self.is_new or clash.id != self.item.id):
                return self._fail(
                    f"Barcode '{barcode}' already belongs to {clash.name}.", 0,
                    self.barcode_input)

        rate = self._parse_gst_rate()
        if rate is None:
            return self._fail("Enter a GST rate between 0 and 100.", 0, self.gst_combo)

        if self.base_uom_combo.current() is None:
            return self._fail("Choose a base unit of measure.", 1, self.base_uom_combo)

        pack = Decimal(str(self.pack_size.value()))
        if pack <= 0:
            return self._fail("Base units per purchase unit must be greater than zero.",
                              1, self.pack_size)

        minimum = Decimal(str(self.min_level.value()))
        maximum = Decimal(str(self.max_level.value()))
        if maximum and maximum < minimum:
            return self._fail(
                "Maximum level cannot be below the minimum level.", 1, self.max_level)

        fields = dict(
            sku=sku,
            barcode=barcode,
            name=name,
            description=self.description_input.toPlainText().strip() or None,
            category=self.category_combo.current(),
            preferred_supplier=self.supplier_combo.current(),
            hsn_code=self.hsn_input.text().strip() or None,
            gst_rate=rate,
            selling_price=Decimal(str(self.selling_price.value())),
            is_active=self.active_check.isChecked(),
            base_uom=self.base_uom_combo.current(),
            purchase_uom=self.purchase_uom_combo.current() or self.base_uom_combo.current(),
            pack_size=pack,
            default_min_level=minimum,
            default_max_level=maximum,
            is_lot_tracked=self.lot_check.isChecked(),
            is_serial_tracked=self.serial_check.isChecked(),
            shelf_life_days=(int(self.shelf_life.value())
                             if self.lot_check.isChecked() and self.shelf_life.value() > 0
                             else None),
        )

        try:
            with db.atomic():
                if self.is_new:
                    item = Item.create(**fields)
                    auth.record_audit(self.user, "CREATE", "Item", item.id,
                                      f"Created item {item.sku} — {item.name}")
                    self._post_opening_stock(item)
                else:
                    for key, value in fields.items():
                        setattr(self.item, key, value)
                    self.item.save()
                    item = self.item
                    auth.record_audit(self.user, "UPDATE", "Item", item.id,
                                      f"Updated item {item.sku} — {item.name}")
            self.saved_item = item
            self.accept()
        except Exception as exc:
            return self._fail(f"Could not save the item: {exc}")

    def _post_opening_stock(self, item):
        if self.opening_warehouse is None:
            return
        warehouse = self.opening_warehouse.current()
        quantity = Decimal(str(self.opening_qty.value()))
        if warehouse is None or quantity <= 0:
            return
        cost = Decimal(str(self.opening_cost.value()))
        inventory.set_opening_balance(
            item, warehouse, quantity,
            unit_cost=cost if cost > 0 else None,
            user=self.user,
        )
