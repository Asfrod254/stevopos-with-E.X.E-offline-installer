import tempfile
import unittest
from pathlib import Path

from offline_pos import PosDB, display_hotkey, normalize_hotkey


class PosDBTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = PosDB(Path(self.temp_dir.name) / "stevo_pos.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_product(self, name="Tea", stock=5, price=12500):
        self.db.add_product(name, "", price, stock)
        return next(product["id"] for product in self.db.products() if product["name"] == name)

    def test_first_run_admin_login_and_password_change(self):
        self.assertIsNotNone(self.db.authenticate("admin", "admin123"))
        self.assertIsNone(self.db.authenticate("admin", "wrong"))
        self.db.change_password("admin", "newpassword")
        self.assertIsNotNone(self.db.authenticate("admin", "newpassword"))
        self.assertIsNone(self.db.authenticate("admin", "admin123"))

    def test_pending_order_reserves_stock_until_completed(self):
        product_id = self.add_product(stock=5)
        order_no = self.db.create_order([(product_id, 3)], "admin", pending=True)
        product = self.db.products()[0]
        self.assertEqual(product["stock"], 5)
        self.assertEqual(product["available"], 2)
        self.db.complete_order(order_no, "M-Pesa")
        product = self.db.products()[0]
        self.assertEqual(product["stock"], 2)
        self.assertEqual(product["available"], 2)
        self.assertEqual(self.db.orders("paid")[0]["payment_method"], "M-Pesa")

    def test_cancelling_pending_order_releases_reservation(self):
        product_id = self.add_product(stock=2)
        order_no = self.db.create_order([(product_id, 2)], "admin", pending=True)
        self.assertEqual(self.db.products()[0]["available"], 0)
        self.db.cancel_order(order_no)
        self.assertEqual(self.db.products()[0]["available"], 2)

    def test_insufficient_stock_does_not_create_order(self):
        product_id = self.add_product(stock=1)
        with self.assertRaises(ValueError):
            self.db.create_order([(product_id, 2)], "admin", pending=True)
        self.assertEqual(self.db.orders(), [])
        self.assertEqual(self.db.products()[0]["stock"], 1)

    def test_pending_reservations_prevent_overbooking(self):
        product_id = self.add_product(stock=3)
        self.db.create_order([(product_id, 2)], "admin", pending=True)
        with self.assertRaises(ValueError):
            self.db.create_order([(product_id, 2)], "admin", payment_method="Cash")
        self.assertEqual(self.db.products()[0]["available"], 1)

    def test_stock_cannot_be_reduced_below_pending_reservations(self):
        product_id = self.add_product(stock=4)
        self.db.create_order([(product_id, 3)], "admin", pending=True)
        with self.assertRaises(ValueError):
            self.db.adjust_stock(product_id, -2)
        self.db.adjust_stock(product_id, -1)
        self.assertEqual(self.db.products()[0]["available"], 0)

    def test_sales_summary_counts_paid_orders_only(self):
        product_id = self.add_product(price=2500)
        self.db.create_order([(product_id, 2)], "admin", payment_method="Card")
        self.db.create_order([(product_id, 1)], "admin", pending=True)
        count, total, pending = self.db.report_summary()
        self.assertEqual((count, total, pending), (1, 5000, 1))

    def test_export_writes_csv_header_and_sale(self):
        product_id = self.add_product()
        order_no = self.db.create_order([(product_id, 1)], "admin", payment_method="Cash")
        path = Path(self.temp_dir.name) / "sales.csv"
        self.db.export_sales(path)
        contents = path.read_text(encoding="utf-8-sig")
        self.assertIn("Order,Status,Payment", contents)
        self.assertIn(order_no, contents)

    def test_duplicate_users_and_products_are_rejected(self):
        self.db.add_user("clerk", "secret123")
        with self.assertRaises(Exception):
            self.db.add_user("CLERK", "secret456")
        self.add_product()
        with self.assertRaises(Exception):
            self.add_product(name="tea")

    def test_hotkeys_are_normalized_and_persisted(self):
        hotkeys = self.db.get_hotkeys()
        self.assertEqual(hotkeys["checkout"], "<F2>")
        hotkeys["checkout"] = normalize_hotkey("Ctrl+Shift+K")
        self.db.set_hotkeys(hotkeys)
        reopened = PosDB(self.db.path)
        self.assertEqual(reopened.get_hotkeys()["checkout"], "<Control-Shift-k>")
        self.assertEqual(display_hotkey(reopened.get_hotkeys()["checkout"]), "Ctrl+Shift+K")

    def test_hotkeys_reject_duplicates_and_invalid_actions(self):
        with self.assertRaises(ValueError):
            self.db.set_hotkeys({"checkout": "<Control-1>"})
        with self.assertRaises(ValueError):
            self.db.set_hotkeys({"unknown_action": "<Control-u>"})
        with self.assertRaises(ValueError):
            normalize_hotkey("Ctrl+Ctrl+P")


if __name__ == "__main__":
    unittest.main()
