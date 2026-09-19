import datetime
import unittest
from decimal import Decimal

from services import analytics
from web import routes_analytics
from web.app import create_app
from web.context import _jinja
from web.routes_analytics import _change


class WebAnalyticsTest(unittest.TestCase):
    def test_analytics_router_is_registered(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_analytics.router for router in included))
        paths = {getattr(route, "path", None)
                 for route in routes_analytics.router.routes}
        self.assertIn("/analytics", paths)

    def test_period_change_helpers(self):
        self.assertEqual(_change(Decimal("125"), Decimal("100")), Decimal("25"))
        self.assertEqual(_change(Decimal("75"), Decimal("100")), Decimal("-25"))
        self.assertIsNone(_change(Decimal("5"), Decimal("0")))
        start, end, label = analytics.resolve_period(
            analytics.THIS_MONTH, datetime.date(2026, 9, 12))
        self.assertEqual(start, datetime.date(2026, 9, 1))
        self.assertEqual(end, datetime.date(2026, 9, 30))
        self.assertEqual(label, "September 2026")

    def test_analytics_template_parses(self):
        self.assertIsNotNone(_jinja.get_template("analytics.html"))


if __name__ == "__main__":
    unittest.main()
