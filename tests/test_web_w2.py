import unittest
from decimal import Decimal
from types import SimpleNamespace

from database.models import Role
from web.app import create_app
from web.context import _jinja
from web import routes_w2
from web.routes_w2 import _decimal_value, _parse_filter_date


class WebW2Test(unittest.TestCase):
    def test_w2_routes_are_registered_in_application(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_w2.router for router in included))
        paths = {getattr(route, "path", None) for route in routes_w2.router.routes}
        self.assertIn("/inventory/new", paths)
        self.assertIn("/inventory/{item_id}/edit", paths)
        self.assertIn("/logs", paths)
        self.assertIn("/settings", paths)
        self.assertIn("/settings/warehouses", paths)
        self.assertIn("/settings/employees", paths)
        self.assertIn("/settings/users", paths)
        self.assertIn("/settings/users/{user_id}/reset-password", paths)

    def test_numeric_and_date_validation_reject_bad_filters(self):
        self.assertEqual(
            _decimal_value("12.50", "Price", minimum=Decimal("0")),
            Decimal("12.50"),
        )
        with self.assertRaisesRegex(ValueError, "cannot be below"):
            _decimal_value("-1", "Price", minimum=Decimal("0"))
        with self.assertRaisesRegex(ValueError, "valid date"):
            _parse_filter_date("2026-99-12", "From date")

    def test_item_form_exposes_barcode_or_qr_value(self):
        values = {
            "sku": "CHEM-1", "barcode": "QR-CHEM-1", "name": "Chemical",
            "description": "", "category_id": "", "supplier_id": "",
            "hsn_code": "", "gst_rate": "18", "selling_price": "0",
            "base_uom_id": "", "purchase_uom_id": "", "pack_size": "1",
            "min_level": "0", "max_level": "0", "shelf_life_days": "",
            "is_lot_tracked": "", "is_serial_tracked": "", "is_active": "1",
        }
        html = _jinja.get_template("item_form.html").render(
            user=None, tenant=None, item=None, values=values,
            categories=[], suppliers=[], uoms=[], csrf="", error=None,
        )
        self.assertIn("Barcode or QR value", html)
        self.assertIn('value="QR-CHEM-1"', html)

    def test_generated_user_credentials_are_shown_once(self):
        account = SimpleNamespace(
            username="lab.user", full_name="Lab User", role=Role.CLERK,
        )
        html = _jinja.get_template("user_credentials.html").render(
            user=None, tenant=None, account=account, Role=Role,
            action="created", temporary_password="temporary123",
        )
        self.assertIn("temporary123", html)
        self.assertIn("shown <strong>once</strong>", html)


if __name__ == "__main__":
    unittest.main()
