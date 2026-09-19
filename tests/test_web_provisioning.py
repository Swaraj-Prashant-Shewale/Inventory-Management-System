import os
import unittest
from unittest.mock import patch

from services import tenancy
from web import routes_admin


class _ExistingTenantCursor:
    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return (1,)


class _Connection:
    def cursor(self):
        return _ExistingTenantCursor()


class WebProvisioningTest(unittest.TestCase):
    def test_limited_credentials_are_preferred(self):
        environment = {
            "PROVISION_DB_USER": "inventory_provisioner.project",
            "PROVISION_DB_PASSWORD": "limited-secret",
            "ADMIN_DB_USER": "postgres.project",
            "ADMIN_DB_PASSWORD": "owner-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                routes_admin._server_admin_credentials(),
                ("inventory_provisioner.project", "limited-secret"),
            )
            self.assertTrue(routes_admin._using_limited_provisioner())

    def test_limited_provisioning_skips_owner_only_registry_setup(self):
        with patch.object(tenancy, "ensure_registry") as ensure_registry:
            with self.assertRaisesRegex(tenancy.TenancyError, "already exists"):
                tenancy.provision_tenant(
                    _Connection(),
                    "limited-secret",
                    slug="example_client",
                    display_name="Example Client",
                    owner_username="example_owner",
                    owner_full_name="Example Owner",
                    ensure_registry_first=False,
                )
        ensure_registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
