import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_FILE"] = str(Path(_TEMP_DIR.name) / "returns.db")

import config  # noqa: E402
from database.connection import close  # noqa: E402
from database.models import (  # noqa: E402
    Customer,
    Item,
    Role,
    Serial,
    SerialStatus,
    Supplier,
    Uom,
    User,
    Warehouse,
)
from services import auth, bootstrap, inventory, purchasing, sales  # noqa: E402
from services import search as search_service  # noqa: E402


class ReturnLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other test modules can initialize the shared database proxy first. Rebind it
        # to this module's disposable database so the lifecycle remains self-contained.
        config.DB_BACKEND = "sqlite"
        config.SQLITE_PATH = Path(_TEMP_DIR.name) / "returns.db"
        bootstrap.initialize()
        cls.warehouse = Warehouse.create(code="RET", name="Returns Lab")
        cls.user = User.create(
            username="returns.owner",
            password_hash=auth.hash_password("Return-Test-Password-123"),
            full_name="Returns Owner",
            role=Role.OWNER,
            warehouse=cls.warehouse,
        )
        cls.supplier = Supplier.create(code="RET-SUP", name="Return Supplier")
        cls.customer = Customer.create(code="RET-CUS", name="Return Customer")
        cls.each = Uom.get(Uom.code == "EA")

    @classmethod
    def tearDownClass(cls):
        close()
        _TEMP_DIR.cleanup()

    def test_purchase_customer_and_supplier_return_lifecycle(self):
        standard = Item.create(
            sku="RET-STD", name="Return Standard", base_uom=self.each,
            selling_price="20",
        )
        serialized = Item.create(
            sku="RET-SER", name="Return Serialized", base_uom=self.each,
            selling_price="50", is_serial_tracked=True,
        )

        purchase = purchasing.create_purchase_order(
            self.supplier, self.warehouse,
            [
                {"item": standard, "quantity": 10, "unit_cost": 10},
                {"item": serialized, "quantity": 2, "unit_cost": 30},
            ],
            user=self.user,
        )
        purchasing.confirm_order(purchase, user=self.user)
        purchase_lines = {line.item_id: line for line in purchase.lines}
        purchasing.receive_goods(
            purchase,
            [
                {"order_line": purchase_lines[standard.id], "quantity": 10,
                 "unit_cost": 10},
                {"order_line": purchase_lines[serialized.id], "quantity": 2,
                 "unit_cost": 30, "serials": ["RET-S1", "RET-S2"]},
            ],
            user=self.user,
        )

        order = sales.create_sales_order(
            self.customer, self.warehouse,
            [
                {"item": standard, "quantity": 4, "unit_price": 20},
                {"item": serialized, "quantity": 1, "unit_price": 50},
            ],
            user=self.user,
        )
        sales.confirm_order(order, user=self.user)
        order_lines = {line.item_id: line for line in order.lines}
        sales.fulfill(
            order,
            [
                {"order_line": order_lines[standard.id], "quantity": 4},
                {"order_line": order_lines[serialized.id], "quantity": 1,
                 "serials": ["RET-S1"]},
            ],
            user=self.user,
        )

        customer_return = sales.create_customer_return(
            self.customer, self.warehouse,
            [
                {"item": standard, "quantity": 2, "unit_price": 20},
                {"item": serialized, "quantity": 1, "unit_price": 50,
                 "serials": ["RET-S1"]},
            ],
            user=self.user, order=order, reason="Unused", restock=True,
        )
        self.assertEqual(inventory.on_hand(standard, self.warehouse), Decimal("8"))
        self.assertEqual(inventory.on_hand(serialized, self.warehouse), Decimal("2"))
        self.assertEqual(
            Serial.get(Serial.serial_number == "RET-S1").status,
            SerialStatus.IN_STOCK,
        )
        with self.assertRaisesRegex(sales.SalesError, "only 2"):
            sales.create_customer_return(
                self.customer, self.warehouse,
                [{"item": standard, "quantity": 3}],
                user=self.user, order=order, reason="Too many",
            )

        supplier_return = purchasing.create_supplier_return(
            self.supplier, self.warehouse,
            [
                {"item": standard, "quantity": 3, "unit_cost": 10},
                {"item": serialized, "quantity": 1, "unit_cost": 30,
                 "serials": ["RET-S2"]},
            ],
            user=self.user, order=purchase, reason="Supplier recall",
        )
        self.assertEqual(inventory.on_hand(standard, self.warehouse), Decimal("5"))
        self.assertEqual(inventory.on_hand(serialized, self.warehouse), Decimal("1"))
        returned_serial = Serial.get(Serial.serial_number == "RET-S2")
        self.assertEqual(returned_serial.status, SerialStatus.RETURNED)
        self.assertIsNone(returned_serial.warehouse_id)
        with self.assertRaisesRegex(purchasing.PurchasingError, "only 7"):
            purchasing.create_supplier_return(
                self.supplier, self.warehouse,
                [{"item": standard, "quantity": 8}],
                user=self.user, order=purchase, reason="Too many",
            )

        scrap_order = sales.create_sales_order(
            self.customer, self.warehouse,
            [{"item": serialized, "quantity": 1, "unit_price": 50}],
            user=self.user,
        )
        sales.confirm_order(scrap_order, user=self.user)
        sales.fulfill(
            scrap_order,
            [{"order_line": scrap_order.lines.get(), "quantity": 1,
              "serials": ["RET-S1"]}],
            user=self.user,
        )
        sales.create_customer_return(
            self.customer, self.warehouse,
            [{"item": serialized, "quantity": 1, "serials": ["RET-S1"]}],
            user=self.user, order=scrap_order, reason="Damaged", restock=False,
        )
        scrapped_serial = Serial.get(Serial.serial_number == "RET-S1")
        self.assertEqual(scrapped_serial.status, SerialStatus.SCRAPPED)
        self.assertIsNone(scrapped_serial.warehouse_id)
        self.assertEqual(inventory.on_hand(serialized, self.warehouse), Decimal("0"))

        found = search_service.search(customer_return.number)
        self.assertTrue(any(result.kind == search_service.CUSTOMER_RETURN
                            and result.payload.id == customer_return.id
                            for result in found))
        found = search_service.search(supplier_return.number)
        self.assertTrue(any(result.kind == search_service.SUPPLIER_RETURN
                            and result.payload.id == supplier_return.id
                            for result in found))


if __name__ == "__main__":
    unittest.main()
