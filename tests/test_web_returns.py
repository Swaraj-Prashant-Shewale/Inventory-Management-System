import unittest
from decimal import Decimal

from web import routes_returns
from web.app import create_app
from web.context import _jinja
from web.routes_returns import _decimal, _serials


class WebReturnsTest(unittest.TestCase):
    def test_return_router_is_registered(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_returns.router for router in included))
        paths = {getattr(route, "path", None)
                 for route in routes_returns.router.routes}
        expected = {
            "/operations/returns",
            "/operations/returns/customer/new",
            "/operations/returns/customer",
            "/operations/returns/customer/{return_id}",
            "/operations/returns/supplier/new",
            "/operations/returns/supplier",
            "/operations/returns/supplier/{return_id}",
        }
        self.assertTrue(expected.issubset(paths))

    def test_return_inputs_validate_quantity_and_serials(self):
        self.assertEqual(_decimal("2.5", "Quantity"), Decimal("2.5"))
        self.assertEqual(_serials("S-1\nS-2, S-3"), ["S-1", "S-2", "S-3"])
        with self.assertRaisesRegex(ValueError, "only once"):
            _serials("S-1,S-1")
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            _decimal("-1", "Quantity")

    def test_return_templates_parse(self):
        for name in ("returns.html", "return_form.html", "return_detail.html"):
            with self.subTest(template=name):
                self.assertIsNotNone(_jinja.get_template(name))


if __name__ == "__main__":
    unittest.main()
