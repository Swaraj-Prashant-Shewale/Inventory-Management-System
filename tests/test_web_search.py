import unittest
from types import SimpleNamespace

from services import auth
from services import search as search_service
from web import routes_search
from web.app import create_app
from web.context import _jinja
from web.routes_search import result_url


class _Context:
    def __init__(self, permissions=()):
        self.permissions = set(permissions)

    def can(self, permission):
        return permission in self.permissions


class WebSearchTest(unittest.TestCase):
    def test_search_router_is_registered(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_search.router for router in included))
        paths = {getattr(route, "path", None) for route in routes_search.router.routes}
        self.assertIn("/search", paths)

    def test_result_destinations_use_existing_web_screens(self):
        context = _Context({auth.PERM_MANAGE_EMPLOYEES})
        item = SimpleNamespace(sku="CHEM A/1", is_active=True)
        order = SimpleNamespace(id=17)
        employee = SimpleNamespace(id=3)
        self.assertEqual(
            result_url(search_service.Result(
                search_service.ITEM, "Chemical", "", item), context),
            "/inventory?q=CHEM+A%2F1",
        )
        self.assertEqual(
            result_url(search_service.Result(
                search_service.SALES_ORDER, "SO", "", order), context),
            "/shipping/17",
        )
        self.assertEqual(
            result_url(search_service.Result(
                search_service.CUSTOMER_RETURN, "CR", "", order), context),
            "/operations/returns/customer/17",
        )
        self.assertEqual(
            result_url(search_service.Result(
                search_service.SUPPLIER_RETURN, "SR", "", order), context),
            "/operations/returns/supplier/17",
        )
        self.assertEqual(
            result_url(search_service.Result(
                search_service.EMPLOYEE, "User", "", employee), context),
            "/settings?tab=employees",
        )

    def test_search_template_parses(self):
        self.assertIsNotNone(_jinja.get_template("search.html"))


if __name__ == "__main__":
    unittest.main()
