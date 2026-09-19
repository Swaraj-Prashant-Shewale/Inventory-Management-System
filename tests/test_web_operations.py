import unittest
from decimal import Decimal

from web import routes_operations
from web.app import create_app
from web.context import _jinja
from web.routes_operations import _date, _decimal, _line_rows


class WebOperationsTest(unittest.TestCase):
    def test_operations_router_is_registered_with_all_state_transitions(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_operations.router for router in included))
        paths = {getattr(route, "path", None)
                 for route in routes_operations.router.routes}
        expected = {
            "/operations", "/operations/adjustments/new",
            "/operations/adjustments", "/operations/adjustments/{adjustment_id}",
            "/operations/adjustments/{adjustment_id}/approve",
            "/operations/transfers/new", "/operations/transfers",
            "/operations/transfers/{transfer_id}",
            "/operations/transfers/{transfer_id}/dispatch",
            "/operations/transfers/{transfer_id}/receive",
            "/operations/counts", "/operations/counts/{count_id}",
        }
        self.assertTrue(expected.issubset(paths))

    def test_signed_and_unsigned_quantity_parsing(self):
        self.assertEqual(_decimal("-2", "Adjustment", signed=True), Decimal("-2"))
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            _decimal("-2", "Transfer")
        self.assertEqual(str(_date("2026-09-12", "Date")), "2026-09-12")

    def test_form_rows_preserve_scanned_values(self):
        rows = _line_rows(["9"], ["-3"], ["Damaged bottle"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["item_id"], "9")
        self.assertEqual(rows[0]["note"], "Damaged bottle")

    def test_all_operation_templates_parse(self):
        names = (
            "operations.html", "adjustment_form.html", "adjustment_detail.html",
            "transfer_form.html", "transfer_detail.html", "receive_transfer.html",
            "count_detail.html",
        )
        for name in names:
            with self.subTest(template=name):
                self.assertIsNotNone(_jinja.get_template(name))


if __name__ == "__main__":
    unittest.main()
