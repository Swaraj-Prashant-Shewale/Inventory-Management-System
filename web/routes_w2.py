"""W2 web screens: item master data, movement logs, and staff settings."""
import datetime
import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from peewee import JOIN, IntegrityError

from database.connection import db
from database.models import (
    Category,
    Customer,
    Direction,
    DocType,
    Employee,
    Item,
    Lot,
    Role,
    StockMovement,
    Supplier,
    Uom,
    User,
    Warehouse,
)
from services import auth, tenancy
from web.context import (
    check_csrf,
    forbidden,
    redirect,
    render,
    require_login,
    resolve_user,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _signed_in(request):
    context = resolve_user(request)
    blocked = require_login(context)
    return context, blocked


def _decimal_value(raw, label, *, minimum=None, maximum=None, blank="0"):
    try:
        value = Decimal((raw or blank).strip() or blank)
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label} must be a number.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} cannot be below {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} cannot be above {maximum:g}.")
    return value


def _optional_record(model, raw, label):
    if not (raw or "").strip():
        return None
    try:
        record = model.get_by_id(int(raw))
    except (ValueError, TypeError, model.DoesNotExist):
        raise ValueError(f"Choose a valid {label}.")
    return record


def _required_record(model, raw, label):
    record = _optional_record(model, raw, label)
    if record is None:
        raise ValueError(f"Choose a {label}.")
    return record


def _item_form_values(**values):
    defaults = {
        "sku": "", "barcode": "", "name": "", "description": "",
        "category_id": "", "supplier_id": "", "hsn_code": "",
        "gst_rate": "18", "selling_price": "0", "base_uom_id": "",
        "purchase_uom_id": "", "pack_size": "1", "min_level": "0",
        "max_level": "0", "shelf_life_days": "", "is_lot_tracked": "",
        "is_serial_tracked": "", "is_active": "1",
    }
    defaults.update(values)
    return defaults


def _item_values_from_record(item):
    return _item_form_values(
        sku=item.sku,
        barcode=item.barcode or "",
        name=item.name,
        description=item.description or "",
        category_id=str(item.category_id or ""),
        supplier_id=str(item.preferred_supplier_id or ""),
        hsn_code=item.hsn_code or "",
        gst_rate=str(item.gst_rate or 0),
        selling_price=str(item.selling_price or 0),
        base_uom_id=str(item.base_uom_id or ""),
        purchase_uom_id=str(item.purchase_uom_id or ""),
        pack_size=str(item.pack_size or 1),
        min_level=str(item.default_min_level or 0),
        max_level=str(item.default_max_level or 0),
        shelf_life_days=str(item.shelf_life_days or ""),
        is_lot_tracked="1" if item.is_lot_tracked else "",
        is_serial_tracked="1" if item.is_serial_tracked else "",
        is_active="1" if item.is_active else "",
    )


def _item_page(context, values, *, item=None, error=None, status_code=200):
    return render(
        context,
        "item_form.html",
        status_code=status_code,
        values=values,
        item=item,
        error=error,
        categories=list(Category.select().where(
            Category.is_active == True).order_by(Category.name)),  # noqa: E712
        suppliers=list(Supplier.select().where(
            Supplier.is_active == True).order_by(Supplier.name)),  # noqa: E712
        uoms=list(Uom.select().order_by(Uom.code)),
    )


def _clean_item(values, item=None):
    sku = (values["sku"] or "").strip().upper()
    name = (values["name"] or "").strip()
    barcode = (values["barcode"] or "").strip() or None
    if not sku:
        raise ValueError("SKU is required.")
    if len(sku) > 40:
        raise ValueError("SKU cannot exceed 40 characters.")
    if not name:
        raise ValueError("Item name is required.")
    if len(name) > 200:
        raise ValueError("Item name cannot exceed 200 characters.")
    if barcode and len(barcode) > 60:
        raise ValueError("Barcode cannot exceed 60 characters.")

    clash = Item.get_or_none(Item.sku == sku)
    if clash is not None and (item is None or clash.id != item.id):
        raise ValueError(f"SKU '{sku}' is already used by {clash.name}.")
    if barcode:
        clash = Item.get_or_none(Item.barcode == barcode)
        if clash is not None and (item is None or clash.id != item.id):
            raise ValueError(f"Barcode '{barcode}' already belongs to {clash.name}.")

    minimum = _decimal_value(values["min_level"], "Minimum level", minimum=Decimal("0"))
    maximum = _decimal_value(values["max_level"], "Maximum level", minimum=Decimal("0"))
    if maximum and maximum < minimum:
        raise ValueError("Maximum level cannot be below the minimum level.")

    lot_tracked = bool(values["is_lot_tracked"])
    shelf_life = None
    if lot_tracked and (values["shelf_life_days"] or "").strip():
        try:
            shelf_life = int(values["shelf_life_days"])
        except ValueError:
            raise ValueError("Shelf life must be a whole number of days.")
        if shelf_life <= 0:
            raise ValueError("Shelf life must be greater than zero.")

    base_uom = _required_record(Uom, values["base_uom_id"], "base unit")
    purchase_uom = _optional_record(Uom, values["purchase_uom_id"], "purchase unit")
    return {
        "sku": sku,
        "barcode": barcode,
        "name": name,
        "description": (values["description"] or "").strip() or None,
        "category": _optional_record(Category, values["category_id"], "category"),
        "preferred_supplier": _optional_record(
            Supplier, values["supplier_id"], "preferred supplier"),
        "hsn_code": (values["hsn_code"] or "").strip() or None,
        "gst_rate": _decimal_value(
            values["gst_rate"], "GST rate", minimum=Decimal("0"),
            maximum=Decimal("100")),
        "selling_price": _decimal_value(
            values["selling_price"], "Selling price", minimum=Decimal("0")),
        "base_uom": base_uom,
        "purchase_uom": purchase_uom or base_uom,
        "pack_size": _decimal_value(
            values["pack_size"], "Pack size", minimum=Decimal("0.001")),
        "default_min_level": minimum,
        "default_max_level": maximum,
        "is_lot_tracked": lot_tracked,
        "is_serial_tracked": bool(values["is_serial_tracked"]),
        "shelf_life_days": shelf_life,
        "is_active": bool(values["is_active"]),
    }


@router.get("/inventory/new")
def item_new_page(request: Request):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_ITEMS):
        return forbidden(context, "create items")
    return _item_page(context, _item_form_values())


@router.get("/inventory/{item_id}/edit")
def item_edit_page(request: Request, item_id: int):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_ITEMS):
        return forbidden(context, "edit items")
    item = Item.get_or_none(Item.id == item_id)
    if item is None:
        return render(context, "error.html", status_code=404,
                      title="Item not found", message="That item no longer exists.")
    return _item_page(context, _item_values_from_record(item), item=item)


def _submitted_item_values(sku, barcode, name, description, category_id,
                           supplier_id, hsn_code, gst_rate, selling_price,
                           base_uom_id, purchase_uom_id, pack_size, min_level,
                           max_level, shelf_life_days, is_lot_tracked,
                           is_serial_tracked, is_active):
    return _item_form_values(
        sku=sku, barcode=barcode, name=name, description=description,
        category_id=category_id, supplier_id=supplier_id, hsn_code=hsn_code,
        gst_rate=gst_rate, selling_price=selling_price,
        base_uom_id=base_uom_id, purchase_uom_id=purchase_uom_id,
        pack_size=pack_size, min_level=min_level, max_level=max_level,
        shelf_life_days=shelf_life_days, is_lot_tracked=is_lot_tracked,
        is_serial_tracked=is_serial_tracked, is_active=is_active,
    )


def _save_item(context, values, item=None):
    fields = _clean_item(values, item=item)
    with db.atomic():
        if item is None:
            item = Item.create(**fields)
            action = "CREATE"
        else:
            for key, value in fields.items():
                setattr(item, key, value)
            item.save()
            action = "UPDATE"
        auth.record_audit(
            context.user, action, "Item", item.id,
            f"{action.title()}d item {item.sku} - {item.name}",
        )
    return item


@router.post("/inventory/new")
@router.post("/inventory/{item_id}/edit")
def item_save(
    request: Request,
    item_id: int = None,
    sku: str = Form(""), barcode: str = Form(""), name: str = Form(""),
    description: str = Form(""), category_id: str = Form(""),
    supplier_id: str = Form(""), hsn_code: str = Form(""),
    gst_rate: str = Form(""), selling_price: str = Form(""),
    base_uom_id: str = Form(""), purchase_uom_id: str = Form(""),
    pack_size: str = Form(""), min_level: str = Form(""),
    max_level: str = Form(""), shelf_life_days: str = Form(""),
    is_lot_tracked: str = Form(""), is_serial_tracked: str = Form(""),
    is_active: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_ITEMS):
        return forbidden(context, "manage items")
    item = Item.get_or_none(Item.id == item_id) if item_id is not None else None
    if item_id is not None and item is None:
        return render(context, "error.html", status_code=404,
                      title="Item not found", message="That item no longer exists.")
    values = _submitted_item_values(
        sku, barcode, name, description, category_id, supplier_id, hsn_code,
        gst_rate, selling_price, base_uom_id, purchase_uom_id, pack_size,
        min_level, max_level, shelf_life_days, is_lot_tracked,
        is_serial_tracked, is_active,
    )
    if not check_csrf(context, csrf_token):
        return _item_page(context, values, item=item,
                          error="The form expired. Try again.", status_code=400)
    try:
        saved = _save_item(context, values, item=item)
    except (ValueError, IntegrityError) as exc:
        return _item_page(context, values, item=item, error=str(exc), status_code=400)
    return redirect(context, f"/inventory?saved={saved.sku}")


def _parse_filter_date(raw, label):
    if not (raw or "").strip():
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} must be a valid date.")


@router.get("/logs")
def movement_logs(request: Request, q: str = "", warehouse: int = 0,
                  direction: str = "", doc_type: str = "",
                  date_from: str = "", date_to: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked

    error = None
    try:
        start = _parse_filter_date(date_from, "From date")
        end = _parse_filter_date(date_to, "To date")
        if start and end and end < start:
            raise ValueError("To date cannot be before From date.")
    except ValueError as exc:
        start = end = None
        error = str(exc)

    query = (StockMovement
             .select(StockMovement, Item, Warehouse, User, Employee,
                     Supplier, Customer, Lot)
             .join(Item)
             .switch(StockMovement).join(Warehouse)
             .switch(StockMovement).join(User, JOIN.LEFT_OUTER)
             .switch(StockMovement).join(Employee, JOIN.LEFT_OUTER)
             .switch(StockMovement).join(Supplier, JOIN.LEFT_OUTER)
             .switch(StockMovement).join(Customer, JOIN.LEFT_OUTER)
             .switch(StockMovement).join(Lot, JOIN.LEFT_OUTER))
    if warehouse:
        query = query.where(StockMovement.warehouse == warehouse)
    if direction in (Direction.IN, Direction.OUT):
        query = query.where(StockMovement.direction == direction)
    else:
        direction = ""
    if doc_type in DocType.LABELS:
        query = query.where(StockMovement.doc_type == doc_type)
    else:
        doc_type = ""
    if start:
        query = query.where(StockMovement.timestamp >= datetime.datetime.combine(
            start, datetime.time.min))
    if end:
        query = query.where(StockMovement.timestamp < datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.where(
            (Item.name ** like) | (Item.sku ** like)
            | (StockMovement.doc_number ** like)
            | (StockMovement.truck_hsrp ** like))
    movements = list(query.order_by(
        StockMovement.timestamp.desc(), StockMovement.id.desc()).limit(500))
    return render(
        context, "logs.html", movements=movements,
        warehouses=list(Warehouse.select().order_by(Warehouse.name)),
        q=term, selected_warehouse=warehouse, direction=direction,
        doc_type=doc_type, date_from=date_from, date_to=date_to,
        doc_labels=DocType.LABELS, Direction=Direction,
        show_cost=context.can(auth.PERM_VIEW_COST), error=error,
    )


def _settings_page(context, tab="", *, error=None, saved="", status_code=200):
    allowed = []
    if context.can(auth.PERM_MANAGE_SETTINGS):
        allowed.append("warehouses")
    if context.can(auth.PERM_MANAGE_EMPLOYEES):
        allowed.append("employees")
    if context.can(auth.PERM_MANAGE_USERS):
        allowed.append("users")
    if not allowed:
        return forbidden(context, "manage settings")
    if tab not in allowed:
        tab = allowed[0]
    return render(
        context, "settings.html", status_code=status_code,
        tab=tab, allowed_tabs=allowed, error=error, saved=saved,
        warehouses=list(Warehouse.select().order_by(Warehouse.name)),
        employees=list(Employee.select().order_by(Employee.name)),
        users=list(User.select().order_by(User.full_name)),
    )


@router.get("/settings")
def settings_page(request: Request, tab: str = "", saved: str = ""):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    return _settings_page(context, tab, saved=(saved or "").strip())


@router.post("/settings/warehouses")
def create_warehouse(
    request: Request, code: str = Form(""), name: str = Form(""),
    state_code: str = Form(""), address_line1: str = Form(""),
    city: str = Form(""), pincode: str = Form(""), phone: str = Form(""),
    csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_SETTINGS):
        return forbidden(context, "create warehouses")
    if not check_csrf(context, csrf_token):
        return _settings_page(context, "warehouses", error="The form expired. Try again.",
                              status_code=400)
    code = (code or "").strip().upper()
    name = (name or "").strip()
    state_code = (state_code or "").strip()
    error = None
    if not code or not name:
        error = "Warehouse code and name are required."
    elif len(code) > 20:
        error = "Warehouse code cannot exceed 20 characters."
    elif state_code and (len(state_code) != 2 or not state_code.isdigit()):
        error = "GST state code must contain two digits, such as 27."
    elif Warehouse.get_or_none(Warehouse.code == code):
        error = f"Warehouse code '{code}' is already in use."
    if error:
        return _settings_page(context, "warehouses", error=error, status_code=400)
    try:
        record = Warehouse.create(
            code=code, name=name, state_code=state_code or None,
            address_line1=(address_line1 or "").strip() or None,
            city=(city or "").strip() or None,
            pincode=(pincode or "").strip() or None,
            phone=(phone or "").strip() or None,
        )
    except IntegrityError as exc:
        log.info("Warehouse create rejected: %s", exc)
        return _settings_page(context, "warehouses",
                              error="That warehouse code is already in use.", status_code=400)
    auth.record_audit(context.user, "CREATE", "Warehouse", record.id,
                      f"Created warehouse {record.code} - {record.name}")
    return redirect(context, f"/settings?tab=warehouses&saved={record.code}")


@router.post("/settings/employees")
def create_employee(
    request: Request, code: str = Form(""), name: str = Form(""),
    designation: str = Form(""), warehouse_id: str = Form(""),
    phone: str = Form(""), email: str = Form(""), joined_on: str = Form(""),
    csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_EMPLOYEES):
        return forbidden(context, "create employees")
    if not check_csrf(context, csrf_token):
        return _settings_page(context, "employees", error="The form expired. Try again.",
                              status_code=400)
    code = (code or "").strip().upper()
    name = (name or "").strip()
    error = None
    try:
        warehouse = _optional_record(Warehouse, warehouse_id, "warehouse")
        joined = _parse_filter_date(joined_on, "Joined on")
    except ValueError as exc:
        error = str(exc)
        warehouse = joined = None
    if not code or not name:
        error = "Badge code and full name are required."
    elif len(code) > 30:
        error = "Badge code cannot exceed 30 characters."
    elif Employee.get_or_none(Employee.code == code):
        error = f"Badge code '{code}' is already in use."
    if error:
        return _settings_page(context, "employees", error=error, status_code=400)
    try:
        record = Employee.create(
            code=code, name=name,
            designation=(designation or "").strip() or None,
            warehouse=warehouse, phone=(phone or "").strip() or None,
            email=(email or "").strip() or None, joined_on=joined,
        )
    except IntegrityError:
        return _settings_page(context, "employees",
                              error="That badge code is already in use.", status_code=400)
    auth.record_audit(context.user, "CREATE", "Employee", record.id,
                      f"Created employee {record.code} - {record.name}")
    return redirect(context, f"/settings?tab=employees&saved={record.code}")


@router.post("/settings/users")
def create_user(
    request: Request, username: str = Form(""), full_name: str = Form(""),
    role: str = Form(Role.CLERK), warehouse_id: str = Form(""),
    employee_id: str = Form(""), csrf_token: str = Form(""),
):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    if not context.can(auth.PERM_MANAGE_USERS):
        return forbidden(context, "create user accounts")
    if not check_csrf(context, csrf_token):
        return _settings_page(context, "users", error="The form expired. Try again.",
                              status_code=400)
    username = (username or "").strip()
    full_name = (full_name or "").strip()
    error = None
    if not username or not full_name:
        error = "Username and full name are required."
    elif len(username) > 60:
        error = "Username cannot exceed 60 characters."
    elif role not in Role.ALL:
        error = "Choose a valid role."
    elif User.get_or_none(User.username == username):
        error = f"Username '{username}' already exists."
    try:
        warehouse = _optional_record(Warehouse, warehouse_id, "warehouse")
        employee = _optional_record(Employee, employee_id, "employee")
    except ValueError as exc:
        error = error or str(exc)
        warehouse = employee = None
    if error:
        return _settings_page(context, "users", error=error, status_code=400)

    temporary = auth.generate_temp_password()
    try:
        with db.atomic():
            if tenancy.is_multitenant():
                tenancy.register_username(username, context.tenant)
            record = User.create(
                username=username, full_name=full_name, role=role,
                warehouse=warehouse, employee=employee,
                password_hash=auth.hash_password(temporary),
                must_change_password=True,
            )
            auth.record_audit(context.user, "CREATE", "User", record.id,
                              f"Created user '{record.username}' as {record.role}")
    except (IntegrityError, tenancy.TenancyError) as exc:
        return _settings_page(context, "users", error=str(exc), status_code=400)
    return render(context, "user_credentials.html", account=record,
                  temporary_password=temporary, action="created")


def _get_managed_user(context, user_id):
    if not context.can(auth.PERM_MANAGE_USERS):
        return None, forbidden(context, "manage user accounts")
    record = User.get_or_none(User.id == user_id)
    if record is None:
        return None, render(context, "error.html", status_code=404,
                            title="User not found", message="That account no longer exists.")
    return record, None


@router.post("/settings/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, csrf_token: str = Form("")):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    record, problem = _get_managed_user(context, user_id)
    if problem is not None:
        return problem
    if not check_csrf(context, csrf_token):
        return _settings_page(context, "users", error="The form expired. Try again.",
                              status_code=400)
    if record.id == context.user.id:
        return _settings_page(context, "users",
                              error="You cannot deactivate the account you are using.",
                              status_code=400)
    if record.is_active and record.role == Role.OWNER:
        others = (User.select().where(
            (User.role == Role.OWNER) & (User.is_active == True)  # noqa: E712
            & (User.id != record.id)).count())
        if not others:
            return _settings_page(
                context, "users",
                error="This is the only active Owner. Create another Owner first.",
                status_code=400)
    record.is_active = not record.is_active
    record.save(only=[User.is_active, User.updated_at])
    action = "Activated" if record.is_active else "Deactivated"
    auth.record_audit(context.user, "UPDATE", "User", record.id,
                      f"{action} user '{record.username}'")
    return redirect(context, f"/settings?tab=users&saved={record.username}")


@router.post("/settings/users/{user_id}/reset-password")
def reset_user_password(request: Request, user_id: int,
                        csrf_token: str = Form("")):
    context, blocked = _signed_in(request)
    if blocked is not None:
        return blocked
    record, problem = _get_managed_user(context, user_id)
    if problem is not None:
        return problem
    if not check_csrf(context, csrf_token):
        return _settings_page(context, "users", error="The form expired. Try again.",
                              status_code=400)
    temporary = auth.generate_temp_password()
    record.password_hash = auth.hash_password(temporary)
    record.must_change_password = True
    record.failed_attempts = 0
    record.locked_until = None
    record.save(only=[User.password_hash, User.must_change_password,
                      User.failed_attempts, User.locked_until, User.updated_at])
    auth.record_audit(context.user, "PASSWORD_RESET", "User", record.id,
                      f"Reset password for '{record.username}'")
    return render(context, "user_credentials.html", account=record,
                  temporary_password=temporary, action="reset")
