import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import manage_platform
from services import tenancy


class _RecordingCursor:
    def __init__(self, existing_role=None):
        self.statements = []
        self.existing_role = existing_role

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params))

    def fetchone(self):
        return self.existing_role


class _Connection:
    def __init__(self, existing_role=None):
        self.cursor_instance = _RecordingCursor(existing_role)

    def cursor(self):
        return self.cursor_instance


class ProvisionerSetupTest(unittest.TestCase):
    def test_setup_creates_non_superuser_role_and_prints_pooler_credentials(self):
        connection = _Connection()
        output = io.StringIO()
        with patch.object(
            manage_platform, "admin_connection", return_value=(connection, "owner")
        ), patch.object(tenancy, "ensure_registry"), patch.object(
            manage_platform.secrets, "token_urlsafe", return_value="generated-secret"
        ), redirect_stdout(output):
            manage_platform.cmd_setup_web_provisioner(SimpleNamespace())

        sql_text = "\n".join(
            statement for statement, _ in connection.cursor_instance.statements
        )
        self.assertIn("CREATE ROLE", sql_text)
        self.assertIn("NOSUPERUSER", sql_text)
        self.assertIn("NOCREATEDB", sql_text)
        self.assertIn("NOCREATEROLE", sql_text)
        self.assertEqual(sql_text.count("CREATE POLICY"), 2)
        self.assertIn("REVOKE ALL", sql_text)
        self.assertIn("PROVISION_DB_USER=inventory_provisioner.", output.getvalue())
        self.assertIn("PROVISION_DB_PASSWORD=generated-secret", output.getvalue())

    def test_rotation_changes_only_password_after_privileges_are_verified(self):
        connection = _Connection(existing_role=(False, False, False, False))
        with patch.object(
            manage_platform, "admin_connection", return_value=(connection, "owner")
        ), patch.object(tenancy, "ensure_registry"), patch.object(
            manage_platform.secrets, "token_urlsafe", return_value="rotated-secret"
        ), redirect_stdout(io.StringIO()):
            manage_platform.cmd_setup_web_provisioner(SimpleNamespace())

        alter = next(
            statement for statement, _ in connection.cursor_instance.statements
            if "ALTER ROLE" in statement
        )
        self.assertNotIn("NOSUPERUSER", alter)
        self.assertNotIn("NOCREATEDB", alter)
        self.assertNotIn("NOCREATEROLE", alter)


if __name__ == "__main__":
    unittest.main()