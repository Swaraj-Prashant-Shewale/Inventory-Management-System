import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_FILE"] = str(Path(_TEMP_DIR.name) / "lab-stock.db")

from database.connection import close  # noqa: E402
from database.models import (  # noqa: E402
    Item,
    LotStock,
    Role,
    Serial,
    SerialStatus,
    Uom,
    User,
    Warehouse,
)
from services import auth, bootstrap, inventory, lab_stock  # noqa: E402


class LabStockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap.initialize()
        cls.warehouse = Warehouse.create(code="LAB", name="Main Lab")
        cls.user = User.create(
            username="lab.clerk",
            password_hash=auth.hash_password("test-password"),
            full_name="Lab Clerk",
            role=Role.CLERK,
            warehouse=cls.warehouse,
        )
        cls.each = Uom.get(Uom.code == "EA")

    @classmethod
    def tearDownClass(cls):
        close()
        _TEMP_DIR.cleanup()

    def setUp(self):
        Serial.delete().execute()
        LotStock.delete().execute()
        Item.delete().execute()

    def _item(self, sku="CHEM-001", **values):
        return Item.create(
            sku=sku,
            barcode=f"BAR-{sku}",
            name=sku,
            base_uom=self.each,
            **values,
        )

    def _prepare(self, direction, item, quantity, **values):
        return lab_stock.prepare(
            direction,
            item.sku,
            self.warehouse.id,
            str(quantity),
            user=self.user,
            **values,
        )

    def test_check_in_and_out_update_one_ledger(self):
        item = self._item()
        lab_stock.commit(self._prepare(
            "IN", item, 5, unit_cost="12.50", notes="New delivery"
        ))
        self.assertEqual(inventory.on_hand(item, self.warehouse), Decimal("5"))
        item = Item.get_by_id(item.id)
        self.assertEqual(item.avg_cost, Decimal("12.5"))

        lab_stock.commit(self._prepare(
            "OUT", item, 2, notes="Experiment A"
        ))
        self.assertEqual(inventory.on_hand(item, self.warehouse), Decimal("3"))

        with self.assertRaises(inventory.InsufficientStock):
            lab_stock.prepare(
                "OUT", item.sku, self.warehouse.id, "4", user=self.user
            )
        self.assertEqual(inventory.on_hand(item, self.warehouse), Decimal("3"))

    def test_batch_stock_checks_out_fefo(self):
        item = self._item(is_lot_tracked=True)
        lab_stock.commit(self._prepare(
            "IN", item, 3, lot_number="BATCH-1", expiry_date="2027-01-31"
        ))
        lab_stock.commit(self._prepare("OUT", item, 2))

        self.assertEqual(inventory.on_hand(item, self.warehouse), Decimal("1"))
        self.assertEqual(
            LotStock.get(LotStock.warehouse == self.warehouse).quantity,
            Decimal("1"),
        )

    def test_serials_must_match_quantity_and_warehouse(self):
        item = self._item(is_serial_tracked=True)
        lab_stock.commit(self._prepare(
            "IN", item, 2, serials="SER-1, SER-2"
        ))
        lab_stock.commit(self._prepare("OUT", item, 1, serials="SER-1"))

        self.assertEqual(inventory.on_hand(item, self.warehouse), Decimal("1"))
        self.assertEqual(
            Serial.get(Serial.serial_number == "SER-1").status,
            SerialStatus.SHIPPED,
        )
        with self.assertRaises(lab_stock.LabStockError):
            self._prepare("OUT", item, 1, serials="SER-1")

    def test_discrete_item_rejects_fractional_quantity(self):
        item = self._item()
        with self.assertRaises(lab_stock.LabStockError):
            self._prepare("IN", item, "0.5")


if __name__ == "__main__":
    unittest.main()
