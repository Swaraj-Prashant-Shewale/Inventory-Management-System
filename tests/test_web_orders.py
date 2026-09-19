import unittest
from decimal import Decimal

from web import routes_orders
from web.app import create_app
from web.context import _jinja
from web.routes_orders import _date, _decimal, _split_serials, _submitted_lines


class WebOrdersTest(unittest.TestCase):
    def test_order_router_is_registered_with_full_workflows(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_orders.router for router in included))
        paths = {getattr(route, "path", None) for route in routes_orders.router.routes}
        expected = {
            "/receiving", "/receiving/orders/new", "/receiving/orders",
            "/receiving/{order_id}", "/receiving/{order_id}/confirm",
            "/receiving/{order_id}/receive", "/receiving/suppliers/new",
            "/shipping", "/shipping/orders/new", "/shipping/orders",
            "/shipping/{order_id}", "/shipping/{order_id}/confirm",
            "/shipping/{order_id}/fulfill", "/shipping/customers/new",
        }
        self.assertTrue(expected.issubset(paths))

    def test_order_input_helpers_preserve_rows_and_validate_values(self):
        rows = _submitted_lines(["4"], ["2.5"], ["12"], ["5"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["item_id"], "4")
        self.assertEqual(_decimal("2.5", "Quantity"), Decimal("2.5"))
        self.assertEqual(str(_date("2026-09-12", "Date")), "2026-09-12")
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            _decimal("-1", "Quantity")

    def test_serial_scanner_input_accepts_lines_and_commas(self):
        self.assertEqual(
            _split_serials("SER-1\nSER-2, SER-3\r\n"),
            ["SER-1", "SER-2", "SER-3"],
        )

    def test_all_order_templates_parse(self):
        names = (
            "receiving.html", "purchase_form.html", "purchase_detail.html",
            "receive_order.html", "shipping.html", "sales_form.html",
            "sales_detail.html", "fulfill_order.html", "partner_form.html",
        )
        for name in names:
            with self.subTest(template=name):
                self.assertIsNotNone(_jinja.get_template(name))


if __name__ == "__main__":
    unittest.main()
