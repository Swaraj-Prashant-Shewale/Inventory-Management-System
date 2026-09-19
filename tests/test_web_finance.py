import datetime
import unittest
from decimal import Decimal

from web import routes_finance
from web.app import create_app
from web.context import _jinja
from web.routes_finance import _payment_amount, _payment_date


class WebFinanceTest(unittest.TestCase):
    def test_payment_router_is_registered(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_finance.router for router in included))
        paths = {getattr(route, "path", None)
                 for route in routes_finance.router.routes}
        self.assertIn("/shipping/{order_id}/payments/new", paths)
        self.assertIn("/shipping/{order_id}/payments", paths)

    def test_payment_input_validation(self):
        self.assertEqual(_payment_amount("125.50"), Decimal("125.50"))
        self.assertEqual(_payment_date("2026-09-12"), datetime.date(2026, 9, 12))
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            _payment_amount("0")
        with self.assertRaisesRegex(ValueError, "two decimal"):
            _payment_amount("1.999")
        with self.assertRaisesRegex(ValueError, "valid date"):
            _payment_date("2026-99-12")

    def test_payment_template_parses(self):
        self.assertIsNotNone(_jinja.get_template("payment_form.html"))


if __name__ == "__main__":
    unittest.main()
