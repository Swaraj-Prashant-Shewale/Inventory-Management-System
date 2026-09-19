import unittest

from web import routes_admin
from web.context import _jinja


class AdminOwnerResetTest(unittest.TestCase):
    def test_reset_route_is_registered(self):
        paths = {getattr(route, "path", None) for route in routes_admin.router.routes}
        self.assertIn("/admin/tenants/{slug}/reset-owner", paths)

    def test_reset_result_shows_generated_credentials(self):
        html = _jinja.get_template("admin/owner_reset.html").render(
            slug="example", owner_username="owner", owner_password="temporary"
        )
        self.assertIn("owner", html)
        self.assertIn("temporary", html)
        self.assertIn("shown <b>once</b>", html)


if __name__ == "__main__":
    unittest.main()
