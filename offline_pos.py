"""Self-contained offline point-of-sale application for Windows."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import tkinter as tk
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Iterable, Iterator

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "stevo_pos.sqlite3"
RECEIPT_DIR = DATA_DIR / "receipts"
PAYMENTS = ("Cash", "M-Pesa", "Card")
APP_VERSION = "1.1.0"
HOTKEY_ACTIONS = (
    ("dashboard", "Open dashboard"),
    ("checkout", "Open checkout"),
    ("products", "Open products"),
    ("orders", "Open orders"),
    ("reports", "Open reports"),
    ("users", "Open users"),
    ("settings", "Open settings"),
    ("about", "Open About"),
    ("add_product", "Add product"),
    ("focus_search", "Focus product search"),
    ("refresh", "Refresh current page"),
    ("shortcuts", "Show shortcut list"),
    ("sign_out", "Sign out"),
)
DEFAULT_HOTKEYS = {
    "dashboard": "<Control-1>",
    "checkout": "<F2>",
    "products": "<Control-3>",
    "orders": "<Control-o>",
    "reports": "<Control-r>",
    "users": "",
    "settings": "<Control-comma>",
    "about": "",
    "add_product": "<Control-Shift-p>",
    "focus_search": "<Control-f>",
    "refresh": "<F5>",
    "shortcuts": "<F1>",
    "sign_out": "",
}


def money(cents: int) -> str:
    return f"KSh {cents / 100:,.2f}"


def parse_money(value: str) -> int:
    amount = round(float(value.replace(",", "").strip()) * 100)
    if amount < 0:
        raise ValueError("Price cannot be negative.")
    return amount


def normalize_hotkey(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = [part.strip() for part in value.split("+")]
    modifier_names = {"ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift"}
    modifiers: list[str] = []
    while parts and parts[0].lower() in modifier_names:
        modifier = modifier_names[parts.pop(0).lower()]
        if modifier in modifiers:
            raise ValueError("A modifier can only appear once.")
        modifiers.append(modifier)
    if len(parts) != 1 or not parts[0]:
        raise ValueError("Enter a shortcut such as Ctrl+Shift+P or F2.")
    key = parts[0]
    special_keys = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
                    "space": "space", "tab": "Tab", "backspace": "BackSpace", "delete": "Delete",
                    "comma": "comma", ",": "comma", "period": "period", ".": "period"}
    if key.lower() in special_keys:
        key = special_keys[key.lower()]
    elif re.fullmatch(r"(?i)f(?:[1-9]|1[0-9]|2[0-4])", key):
        key = key.upper()
    elif len(key) == 1 and key.isalnum():
        key = key.lower()
    else:
        raise ValueError("Use a letter, number, F1-F24, or a supported key such as Enter or Space.")
    return f"<{'-'.join((*modifiers, key))}>"


def display_hotkey(sequence: str) -> str:
    if not sequence:
        return "Not set"
    parts = sequence.strip("<>").split("-")
    names = {"Control": "Ctrl", "Shift": "Shift", "Alt": "Alt", "comma": ",", "period": "."}
    return "+".join(names.get(part, part.upper() if re.fullmatch(r"(?i)f\d+", part) else part.upper()) for part in parts)


def _password_hash(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return salt, hashed


class PosDB:
    """SQLite storage and transaction rules for the offline POS."""

    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    role TEXT NOT NULL DEFAULT 'staff',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    sku TEXT NOT NULL DEFAULT '',
                    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
                    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY,
                    order_no TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending','paid','cancelled')),
                    payment_method TEXT,
                    total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
                    cashier TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    paid_at TEXT
                );
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    product_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                """
            )
            count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0:
                salt, hashed = _password_hash("admin123")
                db.execute(
                    "INSERT INTO users(username,salt,password_hash,role) VALUES(?,?,?,'admin')",
                    ("admin", salt, hashed),
                )
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('store_name','Stevo POS')")

    def authenticate(self, username: str, password: str) -> sqlite3.Row | None:
        with self.connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username.strip(),)
            ).fetchone()
        if not user:
            return None
        _, candidate = _password_hash(password, user["salt"])
        return user if secrets.compare_digest(candidate, user["password_hash"]) else None

    def users(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT id,username,role,active FROM users ORDER BY username").fetchall()

    def add_user(self, username: str, password: str, role: str = "staff") -> None:
        username = username.strip()
        if len(username) < 2 or len(password) < 6:
            raise ValueError("Use a username with at least 2 characters and password with at least 6.")
        if role not in ("admin", "staff"):
            raise ValueError("Role must be admin or staff.")
        salt, hashed = _password_hash(password)
        with self.connect() as db:
            db.execute(
                "INSERT INTO users(username,salt,password_hash,role) VALUES(?,?,?,?)",
                (username, salt, hashed, role),
            )

    def change_password(self, username: str, password: str) -> None:
        if len(password) < 6:
            raise ValueError("Password must have at least 6 characters.")
        salt, hashed = _password_hash(password)
        with self.connect() as db:
            db.execute("UPDATE users SET salt=?,password_hash=? WHERE username=?", (salt, hashed, username))

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self.connect() as db:
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))

    def products(self, search: str = "") -> list[sqlite3.Row]:
        pattern = f"%{search.strip()}%"
        with self.connect() as db:
            return db.execute(
                """SELECT p.*, p.stock - COALESCE((SELECT SUM(i.quantity) FROM order_items i
                   JOIN orders o ON o.id=i.order_id WHERE i.product_id=p.id AND o.status='pending'),0) available
                   FROM products p WHERE p.active=1 AND (p.name LIKE ? OR p.sku LIKE ?) ORDER BY p.name""",
                (pattern, pattern),
            ).fetchall()

    def add_product(self, name: str, sku: str, price_cents: int, stock: int) -> None:
        name = name.strip()
        if not name or price_cents < 0 or stock < 0:
            raise ValueError("Enter a product name, valid price, and non-negative stock.")
        with self.connect() as db:
            db.execute(
                "INSERT INTO products(name,sku,price_cents,stock) VALUES(?,?,?,?)",
                (name, sku.strip(), price_cents, stock),
            )

    def adjust_stock(self, product_id: int, amount: int) -> None:
        with self.connect() as db:
            product = db.execute("SELECT stock FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            if not product or product["stock"] + amount < 0:
                raise ValueError("Stock adjustment would make stock negative.")
            reserved = db.execute(
                "SELECT COALESCE(SUM(i.quantity),0) FROM order_items i JOIN orders o ON o.id=i.order_id "
                "WHERE i.product_id=? AND o.status='pending'", (product_id,)
            ).fetchone()[0]
            if product["stock"] + amount < reserved:
                raise ValueError("Stock cannot be reduced below the quantity reserved by pending orders.")
            db.execute("UPDATE products SET stock=stock+? WHERE id=?", (amount, product_id))

    def create_order(self, items: Iterable[tuple[int, int]], cashier: str, pending: bool = False,
                     payment_method: str | None = None) -> str:
        quantities: dict[int, int] = {}
        for product_id, quantity in items:
            if quantity < 1:
                raise ValueError("Order quantities must be positive.")
            quantities[product_id] = quantities.get(product_id, 0) + quantity
        if not quantities:
            raise ValueError("Add at least one item to the basket.")
        if not pending and payment_method not in PAYMENTS:
            raise ValueError("Choose a valid payment method.")
        order_no = datetime.now().strftime("S%y%m%d%H%M%S") + secrets.token_hex(2).upper()
        status = "pending" if pending else "paid"
        with self.connect() as db:
            lines: list[tuple[int, str, int, int]] = []
            total = 0
            for product_id, quantity in quantities.items():
                product = db.execute(
                    """SELECT p.id,p.name,p.price_cents,p.stock - COALESCE((SELECT SUM(i.quantity)
                       FROM order_items i JOIN orders o ON o.id=i.order_id WHERE i.product_id=p.id
                       AND o.status='pending'),0) available FROM products p WHERE p.id=? AND p.active=1""",
                    (product_id,),
                ).fetchone()
                if not product or product["available"] < quantity:
                    raise ValueError(f"Not enough available stock for {product['name'] if product else 'product'}.")
                lines.append((product_id, product["name"], quantity, product["price_cents"]))
                total += quantity * product["price_cents"]
            created = datetime.now().isoformat(sep=" ", timespec="seconds")
            cursor = db.execute(
                "INSERT INTO orders(order_no,status,payment_method,total_cents,cashier,created_at,paid_at) VALUES(?,?,?,?,?,?,?)",
                (order_no, status, payment_method, total, cashier, created, None if pending else created),
            )
            order_id = cursor.lastrowid
            db.executemany(
                "INSERT INTO order_items(order_id,product_id,product_name,quantity,unit_price_cents) VALUES(?,?,?,?,?)",
                [(order_id, *line) for line in lines],
            )
            if not pending:
                for product_id, _, quantity, _ in lines:
                    db.execute("UPDATE products SET stock=stock-? WHERE id=?", (quantity, product_id))
        return order_no

    def complete_order(self, order_no: str, payment_method: str) -> None:
        if payment_method not in PAYMENTS:
            raise ValueError("Choose a valid payment method.")
        with self.connect() as db:
            order = db.execute("SELECT * FROM orders WHERE order_no=? AND status='pending'", (order_no,)).fetchone()
            if not order:
                raise ValueError("This pending order is no longer available.")
            lines = db.execute("SELECT product_id,quantity FROM order_items WHERE order_id=?", (order["id"],)).fetchall()
            for line in lines:
                product = db.execute("SELECT stock FROM products WHERE id=?", (line["product_id"],)).fetchone()
                if not product or product["stock"] < line["quantity"]:
                    raise ValueError("Stock changed and this order can no longer be completed.")
            for line in lines:
                db.execute("UPDATE products SET stock=stock-? WHERE id=?", (line["quantity"], line["product_id"]))
            db.execute(
                "UPDATE orders SET status='paid',payment_method=?,paid_at=? WHERE id=?",
                (payment_method, datetime.now().isoformat(sep=" ", timespec="seconds"), order["id"]),
            )

    def cancel_order(self, order_no: str) -> None:
        with self.connect() as db:
            cursor = db.execute("UPDATE orders SET status='cancelled' WHERE order_no=? AND status='pending'", (order_no,))
            if not cursor.rowcount:
                raise ValueError("Only pending orders can be cancelled.")

    def orders(self, status: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if status and status != "all":
                return db.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            return db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 500").fetchall()

    def order_items(self, order_no: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT i.* FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.order_no=? ORDER BY i.id",
                (order_no,),
            ).fetchall()

    def report_summary(self) -> tuple[int, int, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(total_cents),0) FROM orders WHERE status='paid' AND date(paid_at)=?",
                (today,),
            ).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
            return row[0], row[1], pending

    def export_sales(self, destination: str | Path) -> None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT order_no,status,payment_method,total_cents,cashier,created_at,paid_at FROM orders ORDER BY id DESC"
            ).fetchall()
        with open(destination, "w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(("Order", "Status", "Payment", "Total (KSh)", "Cashier", "Created", "Paid"))
            writer.writerows((row["order_no"], row["status"], row["payment_method"] or "",
                              f"{row['total_cents'] / 100:.2f}", row["cashier"], row["created_at"], row["paid_at"] or "")
                             for row in rows)

    def store_name(self) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='store_name'").fetchone()
            return row[0] if row else "Stevo POS"

    def set_store_name(self, name: str) -> None:
        if not name.strip():
            raise ValueError("Store name cannot be blank.")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('store_name',?)", (name.strip(),))

    def get_hotkeys(self) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='hotkeys'").fetchone()
        if not row:
            return DEFAULT_HOTKEYS.copy()
        try:
            stored = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return DEFAULT_HOTKEYS.copy()
        return {**DEFAULT_HOTKEYS, **{key: value for key, value in stored.items()
                                     if key in DEFAULT_HOTKEYS and isinstance(value, str)}}

    def set_hotkeys(self, hotkeys: dict[str, str]) -> None:
        if set(hotkeys) - set(DEFAULT_HOTKEYS):
            raise ValueError("Unknown shortcut action.")
        normalized = DEFAULT_HOTKEYS | hotkeys
        for sequence in normalized.values():
            if sequence:
                normalize_hotkey(display_hotkey(sequence))
        active = [sequence for sequence in normalized.values() if sequence]
        if len(active) != len(set(active)):
            raise ValueError("Each action must have a different shortcut.")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('hotkeys',?)",
                       (json.dumps(normalized),))


class PosApp:
    def __init__(self, root: tk.Tk, db: PosDB):
        self.root = root
        self.db = db
        self.user: sqlite3.Row | None = None
        self.basket: dict[int, int] = {}
        self.search_var = tk.StringVar()
        self.root.title("Stevo POS | Offline")
        self.root.geometry("1100x700")
        self.root.minsize(850, 560)
        self.root.configure(bg="#f2f4f7")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background="#e8edf2")
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 10))
        style.configure("Primary.TButton", background="#087e70", foreground="white", font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#06695e")])
        self.show_login()

    def clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def show_login(self) -> None:
        self.root.configure(menu="")
        for sequence in getattr(self, "bound_hotkey_sequences", set()):
            self.root.unbind_all(sequence)
        self.bound_hotkey_sequences = set()
        self.user = None
        self.clear()
        panel = tk.Frame(self.root, bg="#f2f4f7")
        panel.place(relx=.5, rely=.48, anchor="center")
        tk.Label(panel, text=self.db.store_name(), bg="#f2f4f7", fg="#163b40",
                 font=("Segoe UI", 25, "bold")).pack(pady=(0, 5))
        tk.Label(panel, text="OFFLINE POINT OF SALE", bg="#f2f4f7", fg="#61747a",
                 font=("Segoe UI", 9, "bold")).pack(pady=(0, 22))
        form = tk.Frame(panel, bg="white", padx=28, pady=25, highlightthickness=1, highlightbackground="#d8e0e4")
        form.pack()
        tk.Label(form, text="Username", bg="white", anchor="w").grid(row=0, column=0, sticky="ew")
        username = ttk.Entry(form, width=31)
        username.grid(row=1, column=0, pady=(5, 15), ipady=5)
        tk.Label(form, text="Password", bg="white", anchor="w").grid(row=2, column=0, sticky="ew")
        password = ttk.Entry(form, width=31, show="•")
        password.grid(row=3, column=0, pady=(5, 18), ipady=5)

        def login(event: object = None) -> None:
            user = self.db.authenticate(username.get(), password.get())
            if not user:
                messagebox.showerror("Sign in failed", "Check your username and password.", parent=self.root)
                password.delete(0, "end")
                password.focus_set()
                return
            self.user = user
            self.show_shell()
            self.show_dashboard()

        ttk.Button(form, text="Sign in", style="Primary.TButton", command=login).grid(row=4, column=0, sticky="ew")
        password.bind("<Return>", login)
        username.bind("<Return>", lambda event: password.focus_set())
        username.focus_set()

    def show_shell(self) -> None:
        self.clear()
        self.build_menu_bar()
        header = tk.Frame(self.root, bg="#163b40", height=58)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=self.db.store_name(), fg="white", bg="#163b40",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=22)
        self.pending_badge = tk.Label(header, text="", fg="#d7f5e7", bg="#163b40", font=("Segoe UI", 10, "bold"))
        self.pending_badge.pack(side="left", padx=20)
        username = self.user["username"]
        identity = username if username.casefold() == self.user["role"].casefold() else f"{username}  ·  {self.user['role']}"
        tk.Label(header, text=identity, fg="#dce7e8", bg="#163b40",
                 font=("Segoe UI", 10)).pack(side="right", padx=(8, 20))
        ttk.Button(header, text="Sign out", command=self.show_login).pack(side="right", padx=6)
        body = tk.Frame(self.root, bg="#f2f4f7")
        body.pack(fill="both", expand=True)
        nav = tk.Frame(body, bg="#e8edf0", width=190)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self.content = tk.Frame(body, bg="#f2f4f7", padx=24, pady=20)
        self.content.pack(side="left", fill="both", expand=True)
        buttons = [("Dashboard", self.show_dashboard), ("Checkout", self.show_checkout),
                   ("Products", self.show_products), ("Orders", self.show_orders), ("Reports", self.show_reports)]
        if self.user["role"] == "admin":
            buttons += [("Users", self.show_users)]
        for text, command in buttons:
            ttk.Button(nav, text=text, command=command).pack(fill="x", padx=12, pady=(12, 0), ipady=3)
        shortcut_key = display_hotkey(self.db.get_hotkeys()["shortcuts"])
        tk.Label(nav, text=f"{shortcut_key}  Shortcuts", bg="#e8edf0", fg="#53656a",
                 font=("Segoe UI", 9)).pack(side="bottom", pady=16)
        self._bind_hotkeys()
        self.update_pending_badge()

    def build_menu_bar(self) -> None:
        hotkeys = self.db.get_hotkeys()
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Sign out", accelerator=display_hotkey(hotkeys["sign_out"]),
                              command=self.show_login)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menu_bar.add_cascade(label="File", menu=file_menu)

        navigate_menu = tk.Menu(menu_bar, tearoff=False)
        for action, label, command in (
            ("dashboard", "Dashboard", self.show_dashboard),
            ("checkout", "Checkout", self.show_checkout),
            ("products", "Products", self.show_products),
            ("orders", "Orders", self.show_orders),
            ("reports", "Reports", self.show_reports),
        ):
            navigate_menu.add_command(label=label, accelerator=display_hotkey(hotkeys[action]), command=command)
        if self.user["role"] == "admin":
            navigate_menu.add_command(label="Users", accelerator=display_hotkey(hotkeys["users"]),
                                      command=self.show_users)
        menu_bar.add_cascade(label="Navigate", menu=navigate_menu)

        tools_menu = tk.Menu(menu_bar, tearoff=False)
        tools_menu.add_command(label="Add product", accelerator=display_hotkey(hotkeys["add_product"]),
                               command=self.add_product_dialog)
        if self.user["role"] == "admin":
            tools_menu.add_command(label="Settings", accelerator=display_hotkey(hotkeys["settings"]),
                                   command=self.show_settings)
        tools_menu.add_command(label="Keyboard shortcuts", accelerator=display_hotkey(hotkeys["shortcuts"]),
                               command=self.show_shortcuts)
        menu_bar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="About Stevo POS", command=self.show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu_bar)
        self.menu_bar = menu_bar

    def _bind_hotkeys(self) -> None:
        for sequence in getattr(self, "bound_hotkey_sequences", set()):
            self.root.unbind_all(sequence)
        self.bound_hotkey_sequences: set[str] = set()
        actions = dict(HOTKEY_ACTIONS)
        for action, sequence in self.db.get_hotkeys().items():
            if sequence and action in actions:
                self.root.bind_all(sequence, lambda event, key=action: self.run_hotkey(key))
                self.bound_hotkey_sequences.add(sequence)

    def run_hotkey(self, action: str) -> None:
        commands = {
            "dashboard": self.show_dashboard,
            "checkout": self.show_checkout,
            "products": self.show_products,
            "orders": self.show_orders,
            "reports": self.show_reports,
            "settings": self.show_settings,
            "about": self.show_about,
            "add_product": self.add_product_dialog,
            "focus_search": self.focus_search,
            "refresh": self.refresh_current,
            "shortcuts": self.show_shortcuts,
            "sign_out": self.show_login,
        }
        if action == "users":
            if self.user and self.user["role"] == "admin":
                self.show_users()
            return
        command = commands.get(action)
        if command:
            if action == "settings" and self.user["role"] != "admin":
                return
            command()

    def page_title(self, title: str, subtitle: str = "") -> None:
        for child in self.content.winfo_children():
            child.destroy()
        tk.Label(self.content, text=title, bg="#f2f4f7", fg="#163b40",
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(self.content, text=subtitle, bg="#f2f4f7", fg="#64777c",
                     font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 15))
        else:
            tk.Frame(self.content, bg="#f2f4f7", height=14).pack()

    def update_pending_badge(self) -> None:
        if hasattr(self, "pending_badge"):
            pending = self.db.report_summary()[2]
            self.pending_badge.configure(text=f"{pending} pending order{'s' if pending != 1 else ''}")

    def show_dashboard(self) -> None:
        self.page_title("Dashboard", datetime.now().strftime("%A, %d %B %Y"))
        count, total, pending = self.db.report_summary()
        metrics = tk.Frame(self.content, bg="#f2f4f7")
        metrics.pack(fill="x", pady=(8, 22))
        for label, value, color in (("SALES TODAY", money(total), "#087e70"),
                                    ("PAID ORDERS", str(count), "#28658a"),
                                    ("PENDING ORDERS", str(pending), "#b76c20")):
            tile = tk.Frame(metrics, bg="white", padx=18, pady=15, highlightthickness=1, highlightbackground="#e0e5e8")
            tile.pack(side="left", fill="x", expand=True, padx=(0, 12))
            tk.Label(tile, text=label, bg="white", fg="#697b80", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            tk.Label(tile, text=value, bg="white", fg=color, font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(7, 0))
        ttk.Button(self.content, text="Open checkout  (F2)", style="Primary.TButton",
                   command=self.show_checkout).pack(anchor="w")
        tk.Label(self.content, text=f"Local database: {self.db.path}", bg="#f2f4f7", fg="#728287",
                 font=("Segoe UI", 9)).pack(anchor="w", side="bottom", pady=8)

    def show_checkout(self) -> None:
        self.page_title("Checkout", "Search for products and add quantities to the basket.")
        main = tk.Frame(self.content, bg="#f2f4f7")
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg="#f2f4f7")
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))
        right = tk.Frame(main, bg="white", padx=16, pady=12, highlightthickness=1, highlightbackground="#dce3e6")
        right.pack(side="right", fill="both", padx=(0, 2))
        searchrow = tk.Frame(left, bg="#f2f4f7")
        searchrow.pack(fill="x", pady=(0, 10))
        self.search_var = tk.StringVar()
        search = ttk.Entry(searchrow, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, ipady=5)
        search.bind("<KeyRelease>", lambda event: self.populate_products())
        self.product_tree = self.tree(left, ("name", "sku", "price", "available"), ("Product", "SKU", "Price", "Available"))
        self.product_tree.column("name", width=180)
        self.product_tree.bind("<Double-1>", lambda event: self.add_selected_product())
        ttk.Button(left, text="Add selected", command=self.add_selected_product).pack(anchor="w", pady=8)
        tk.Label(right, text="Basket", bg="white", fg="#163b40", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.basket_tree = self.tree(right, ("name", "qty", "line"), ("Item", "Qty", "Total"), height=9)
        self.basket_tree.column("name", width=145)
        controls = tk.Frame(right, bg="white")
        controls.pack(fill="x", pady=5)
        ttk.Button(controls, text="−", width=3, command=lambda: self.change_basket(-1)).pack(side="left")
        ttk.Button(controls, text="+", width=3, command=lambda: self.change_basket(1)).pack(side="left", padx=5)
        ttk.Button(controls, text="Remove", command=self.remove_basket_item).pack(side="left")
        self.total_label = tk.Label(right, text=money(0), bg="white", fg="#087e70", font=("Segoe UI", 18, "bold"))
        self.total_label.pack(anchor="e", pady=(8, 12))
        ttk.Button(right, text="Save as pending", command=self.save_pending).pack(fill="x", pady=3)
        ttk.Button(right, text="Take payment", style="Primary.TButton", command=self.take_payment).pack(fill="x", pady=3)
        self.populate_products()
        self.populate_basket()
        self.search_entry = search

    def tree(self, parent: tk.Widget, columns: tuple[str, ...], headings: tuple[str, ...], height: int = 12) -> ttk.Treeview:
        frame = tk.Frame(parent, bg="#f2f4f7")
        frame.pack(fill="both", expand=True)
        widget = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for column, heading in zip(columns, headings):
            widget.heading(column, text=heading)
            widget.column(column, width=105, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return widget

    def populate_products(self) -> None:
        if not hasattr(self, "product_tree") or not self.product_tree.winfo_exists():
            return
        self.product_tree.delete(*self.product_tree.get_children())
        for product in self.db.products(self.search_var.get()):
            self.product_tree.insert("", "end", iid=str(product["id"]), values=(product["name"], product["sku"],
                                    money(product["price_cents"]), product["available"]))

    def add_selected_product(self) -> None:
        selection = self.product_tree.selection()
        if not selection:
            return
        product_id = int(selection[0])
        product = next((p for p in self.db.products() if p["id"] == product_id), None)
        if not product or product["available"] <= self.basket.get(product_id, 0):
            messagebox.showwarning("Stock unavailable", "There is no more available stock for this product.", parent=self.root)
            return
        self.basket[product_id] = self.basket.get(product_id, 0) + 1
        self.populate_basket()

    def populate_basket(self) -> None:
        if not hasattr(self, "basket_tree") or not self.basket_tree.winfo_exists():
            return
        self.basket_tree.delete(*self.basket_tree.get_children())
        products = {p["id"]: p for p in self.db.products()}
        total = 0
        for product_id, quantity in self.basket.items():
            product = products.get(product_id)
            if not product:
                continue
            line = quantity * product["price_cents"]
            total += line
            self.basket_tree.insert("", "end", iid=str(product_id), values=(product["name"], quantity, money(line)))
        self.total_label.configure(text=money(total))

    def change_basket(self, amount: int) -> None:
        selection = self.basket_tree.selection()
        if not selection:
            return
        product_id = int(selection[0])
        next_quantity = self.basket.get(product_id, 0) + amount
        product = next((p for p in self.db.products() if p["id"] == product_id), None)
        if next_quantity < 1:
            self.basket.pop(product_id, None)
        elif product and next_quantity <= product["available"]:
            self.basket[product_id] = next_quantity
        else:
            messagebox.showwarning("Stock unavailable", "Basket quantity exceeds available stock.", parent=self.root)
        self.populate_basket()

    def remove_basket_item(self) -> None:
        selection = self.basket_tree.selection()
        if selection:
            self.basket.pop(int(selection[0]), None)
            self.populate_basket()

    def save_pending(self) -> None:
        self.finish_checkout(pending=True)

    def take_payment(self) -> None:
        if not self.basket:
            messagebox.showwarning("Empty basket", "Add products before taking payment.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Take payment")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        tk.Label(dialog, text=f"Amount due  {self.total_label.cget('text')}", font=("Segoe UI", 14, "bold"), padx=20, pady=16).pack()
        choice = tk.StringVar(value=PAYMENTS[0])
        ttk.Combobox(dialog, textvariable=choice, values=PAYMENTS, state="readonly", width=22).pack(padx=20, pady=(0, 14))
        ttk.Button(dialog, text="Record payment", style="Primary.TButton",
                   command=lambda: (dialog.destroy(), self.finish_checkout(payment=choice.get()))).pack(fill="x", padx=20, pady=(0, 18))

    def finish_checkout(self, pending: bool = False, payment: str | None = None) -> None:
        try:
            order_no = self.db.create_order(self.basket.items(), self.user["username"], pending, payment)
            self.basket.clear()
            if not pending:
                self.write_receipt(order_no)
            self.update_pending_badge()
            self.show_checkout()
            messagebox.showinfo("Order saved", f"Order {order_no} {'saved as pending' if pending else 'paid and recorded'}.", parent=self.root)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save order", str(error), parent=self.root)

    def write_receipt(self, order_no: str) -> None:
        order = next((o for o in self.db.orders() if o["order_no"] == order_no), None)
        if not order:
            return
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        lines = [self.db.store_name(), f"Order: {order_no}", f"Date: {order['paid_at']}", "-" * 36]
        for item in self.db.order_items(order_no):
            lines.append(f"{item['product_name']} x{item['quantity']}  {money(item['quantity'] * item['unit_price_cents'])}")
        lines.extend(("-" * 36, f"Total: {money(order['total_cents'])}", f"Paid by: {order['payment_method']}",
                      f"Cashier: {order['cashier']}"))
        (RECEIPT_DIR / f"{order_no}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def show_products(self) -> None:
        self.page_title("Products", "Manage your catalog and on-hand stock.")
        bar = tk.Frame(self.content, bg="#f2f4f7")
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="Add product", style="Primary.TButton", command=self.add_product_dialog).pack(side="left")
        ttk.Button(bar, text="Adjust selected stock", command=self.adjust_stock_dialog).pack(side="left", padx=8)
        tree = self.tree(self.content, ("name", "sku", "price", "stock", "available"),
                         ("Product", "SKU", "Price", "On hand", "Available"))
        for product in self.db.products():
            tree.insert("", "end", iid=str(product["id"]), values=(product["name"], product["sku"],
                         money(product["price_cents"]), product["stock"], product["available"]))
        self.product_admin_tree = tree

    def add_product_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Add product")
        dialog.transient(self.root)
        dialog.grab_set()
        entries: dict[str, ttk.Entry] = {}
        for row, (label, key) in enumerate((("Product name", "name"), ("SKU / barcode", "sku"),
                                            ("Price (KSh)", "price"), ("Opening stock", "stock"))):
            tk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=18, pady=7)
            entry = ttk.Entry(dialog, width=29)
            entry.grid(row=row, column=1, padx=18, pady=7, ipady=3)
            entries[key] = entry
        entries["stock"].insert(0, "0")

        def save() -> None:
            try:
                self.db.add_product(entries["name"].get(), entries["sku"].get(),
                                    parse_money(entries["price"].get()), int(entries["stock"].get()))
                dialog.destroy()
                if hasattr(self, "product_admin_tree") and self.product_admin_tree.winfo_exists():
                    self.show_products()
                else:
                    self.populate_products()
            except (ValueError, sqlite3.Error) as error:
                messagebox.showerror("Product not saved", str(error), parent=dialog)
        ttk.Button(dialog, text="Save product", style="Primary.TButton", command=save).grid(row=4, column=0, columnspan=2, padx=18, pady=16, sticky="ew")

    def adjust_stock_dialog(self) -> None:
        selection = self.product_admin_tree.selection() if hasattr(self, "product_admin_tree") else ()
        if not selection:
            messagebox.showinfo("Select a product", "Choose a product first.", parent=self.root)
            return
        amount = simpledialog.askinteger("Adjust stock", "Enter stock change (use a negative number to remove stock):", parent=self.root)
        if amount is None:
            return
        try:
            self.db.adjust_stock(int(selection[0]), amount)
            self.show_products()
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Stock not adjusted", str(error), parent=self.root)

    def show_orders(self) -> None:
        self.page_title("Orders", "Review pending orders and recent sales.")
        bar = tk.Frame(self.content, bg="#f2f4f7")
        bar.pack(fill="x", pady=(0, 10))
        status = tk.StringVar(value="all")
        ttk.Label(bar, text="Show").pack(side="left", padx=(0, 6))
        picker = ttk.Combobox(bar, textvariable=status, values=("all", "pending", "paid", "cancelled"), state="readonly", width=14)
        picker.pack(side="left")
        tree = self.tree(self.content, ("order", "status", "payment", "total", "cashier", "date"),
                         ("Order", "Status", "Payment", "Total", "Cashier", "Created"))
        self.orders_tree = tree
        def refresh(event: object = None) -> None:
            tree.delete(*tree.get_children())
            for order in self.db.orders(status.get()):
                tree.insert("", "end", iid=order["order_no"], values=(order["order_no"], order["status"],
                            order["payment_method"] or "-", money(order["total_cents"]), order["cashier"], order["created_at"]))
            self.update_pending_badge()
        picker.bind("<<ComboboxSelected>>", refresh)
        refresh()
        actions = tk.Frame(self.content, bg="#f2f4f7")
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="Complete pending", style="Primary.TButton", command=self.complete_pending_dialog).pack(side="left")
        ttk.Button(actions, text="Cancel pending", command=self.cancel_pending).pack(side="left", padx=8)
        ttk.Button(actions, text="View items", command=self.view_order_items).pack(side="left")

    def selected_order(self) -> str | None:
        selection = self.orders_tree.selection() if hasattr(self, "orders_tree") else ()
        return selection[0] if selection else None

    def complete_pending_dialog(self) -> None:
        order_no = self.selected_order()
        if not order_no:
            messagebox.showinfo("Select an order", "Choose a pending order first.", parent=self.root)
            return
        order = next((o for o in self.db.orders("pending") if o["order_no"] == order_no), None)
        if not order:
            messagebox.showwarning("Not pending", "Select a pending order to complete.", parent=self.root)
            return
        choice = simpledialog.askstring("Complete order", "Payment method: Cash, M-Pesa, or Card", initialvalue="Cash", parent=self.root)
        if not choice:
            return
        payment = next((method for method in PAYMENTS if method.lower() == choice.strip().lower()), None)
        try:
            self.db.complete_order(order_no, payment or "")
            self.write_receipt(order_no)
            self.show_orders()
            messagebox.showinfo("Payment recorded", f"Order {order_no} is paid.", parent=self.root)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not complete order", str(error), parent=self.root)

    def cancel_pending(self) -> None:
        order_no = self.selected_order()
        if not order_no:
            return
        try:
            self.db.cancel_order(order_no)
            self.show_orders()
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not cancel order", str(error), parent=self.root)

    def view_order_items(self) -> None:
        order_no = self.selected_order()
        if not order_no:
            return
        lines = self.db.order_items(order_no)
        detail = "\n".join(f"{line['product_name']}  x{line['quantity']}  {money(line['unit_price_cents'] * line['quantity'])}" for line in lines)
        messagebox.showinfo(f"Order {order_no}", detail or "No items", parent=self.root)

    def show_reports(self) -> None:
        self.page_title("Reports", "Paid sales for today and exportable order history.")
        count, total, pending = self.db.report_summary()
        tk.Label(self.content, text=f"Today's paid sales    {money(total)}", bg="#f2f4f7", fg="#087e70",
                 font=("Segoe UI", 19, "bold")).pack(anchor="w", pady=(14, 8))
        tk.Label(self.content, text=f"Paid orders: {count}     Pending orders: {pending}", bg="#f2f4f7",
                 fg="#53666b", font=("Segoe UI", 11)).pack(anchor="w", pady=(0, 18))
        ttk.Button(self.content, text="Export all orders to CSV", style="Primary.TButton", command=self.export_report).pack(anchor="w")

    def export_report(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv",
               initialfile=f"stevo-sales-{datetime.now():%Y%m%d}.csv", filetypes=(("CSV files", "*.csv"),))
        if path:
            try:
                self.db.export_sales(path)
                messagebox.showinfo("Report exported", f"Saved to:\n{path}", parent=self.root)
            except OSError as error:
                messagebox.showerror("Export failed", str(error), parent=self.root)

    def show_users(self) -> None:
        self.page_title("Users", "Add staff accounts or disable access for an existing account.")
        actions = tk.Frame(self.content, bg="#f2f4f7")
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Add user", style="Primary.TButton", command=self.add_user_dialog).pack(side="left")
        ttk.Button(actions, text="Enable / disable selected", command=self.toggle_user).pack(side="left", padx=8)
        tree = self.tree(self.content, ("username", "role", "status"), ("Username", "Role", "Status"))
        for user in self.db.users():
            tree.insert("", "end", iid=str(user["id"]), values=(user["username"], user["role"], "Active" if user["active"] else "Disabled"))
        self.users_tree = tree

    def add_user_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Add user")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = []
        for row, label in enumerate(("Username", "Password")):
            tk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=16, pady=8)
            entry = ttk.Entry(dialog, width=25, show="•" if label == "Password" else "")
            entry.grid(row=row, column=1, padx=16, pady=8)
            fields.append(entry)
        role = tk.StringVar(value="staff")
        ttk.Combobox(dialog, textvariable=role, values=("staff", "admin"), state="readonly", width=22).grid(row=2, column=1, padx=16, pady=8)
        tk.Label(dialog, text="Role").grid(row=2, column=0, sticky="w", padx=16, pady=8)
        def save() -> None:
            try:
                self.db.add_user(fields[0].get(), fields[1].get(), role.get())
                dialog.destroy()
                self.show_users()
            except (ValueError, sqlite3.Error) as error:
                messagebox.showerror("User not added", str(error), parent=dialog)
        ttk.Button(dialog, text="Create account", style="Primary.TButton", command=save).grid(row=3, column=0, columnspan=2, padx=16, pady=14, sticky="ew")

    def toggle_user(self) -> None:
        selection = self.users_tree.selection() if hasattr(self, "users_tree") else ()
        if not selection:
            return
        user = next((u for u in self.db.users() if u["id"] == int(selection[0])), None)
        if not user:
            return
        if user["username"] == self.user["username"] and user["active"]:
            messagebox.showwarning("Cannot disable", "You cannot disable your own signed-in account.", parent=self.root)
            return
        self.db.set_user_active(user["id"], not bool(user["active"]))
        self.show_users()

    def show_settings(self) -> None:
        self.page_title("Settings", "Store preferences, keyboard shortcuts, and account security.")
        tabs = ttk.Notebook(self.content)
        tabs.pack(fill="both", expand=True)
        general = tk.Frame(tabs, bg="#f2f4f7", padx=18, pady=18)
        hotkeys = tk.Frame(tabs, bg="#f2f4f7", padx=18, pady=18)
        tabs.add(general, text="General")
        tabs.add(hotkeys, text="Hotkeys")

        tk.Label(general, text="Store name", bg="#f2f4f7").grid(row=0, column=0, sticky="w", pady=7)
        store = ttk.Entry(general, width=35)
        store.insert(0, self.db.store_name())
        store.grid(row=0, column=1, padx=12, pady=7, ipady=4)

        def save_store() -> None:
            try:
                self.db.set_store_name(store.get())
                self.show_shell()
                self.show_settings()
            except ValueError as error:
                messagebox.showerror("Settings not saved", str(error), parent=self.root)

        ttk.Button(general, text="Save store name", command=save_store).grid(row=1, column=1, sticky="w", padx=12, pady=5)
        ttk.Button(general, text="Change my password", command=self.change_password_dialog).grid(row=2, column=1, sticky="w", padx=12, pady=5)
        tk.Label(general, text=f"Database file: {self.db.path}\nBack up this file to preserve sales and inventory.",
                 bg="#f2f4f7", fg="#64777c", justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=20)

        tree = self.tree(hotkeys, ("action", "shortcut"), ("Action", "Shortcut"), height=13)
        tree.column("action", width=250)
        tree.column("shortcut", width=180)
        self.hotkeys_tree = tree
        self.populate_hotkeys()
        controls = tk.Frame(hotkeys, bg="#f2f4f7")
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Configure selected", style="Primary.TButton", command=self.configure_hotkey).pack(side="left")
        ttk.Button(controls, text="Reset defaults", command=self.reset_hotkeys).pack(side="left", padx=8)
        tk.Label(hotkeys, text="Enter a key or combination such as Ctrl+Shift+P. Leave blank to clear a shortcut.",
                 bg="#f2f4f7", fg="#64777c").pack(anchor="w", pady=(10, 0))

    def populate_hotkeys(self) -> None:
        if not hasattr(self, "hotkeys_tree") or not self.hotkeys_tree.winfo_exists():
            return
        self.hotkeys_tree.delete(*self.hotkeys_tree.get_children())
        hotkeys = self.db.get_hotkeys()
        for action, title in HOTKEY_ACTIONS:
            self.hotkeys_tree.insert("", "end", iid=action, values=(title, display_hotkey(hotkeys[action])))

    def configure_hotkey(self) -> None:
        selection = self.hotkeys_tree.selection() if hasattr(self, "hotkeys_tree") else ()
        if not selection:
            messagebox.showinfo("Select an action", "Choose an action to configure.", parent=self.root)
            return
        action = selection[0]
        hotkeys = self.db.get_hotkeys()
        title = dict(HOTKEY_ACTIONS)[action]
        value = simpledialog.askstring("Configure shortcut", f"Shortcut for {title}:\nExample: Ctrl+Shift+P",
                                       initialvalue=display_hotkey(hotkeys[action]).replace("Not set", ""), parent=self.root)
        if value is None:
            return
        try:
            sequence = normalize_hotkey(value)
            if sequence and any(key != action and existing == sequence for key, existing in hotkeys.items()):
                raise ValueError("That shortcut is already assigned to another action.")
            hotkeys[action] = sequence
            self.db.set_hotkeys(hotkeys)
            self._bind_hotkeys()
            self.populate_hotkeys()
        except ValueError as error:
            messagebox.showerror("Shortcut not saved", str(error), parent=self.root)

    def reset_hotkeys(self) -> None:
        if not messagebox.askyesno("Reset shortcuts", "Restore the default keyboard shortcuts?", parent=self.root):
            return
        self.db.set_hotkeys(DEFAULT_HOTKEYS.copy())
        self._bind_hotkeys()
        self.populate_hotkeys()

    def show_about(self) -> None:
        self.page_title("About Stevo POS", "Sell simply. Keep your business moving.")
        panel = tk.Frame(self.content, bg="white", padx=24, pady=22,
                         highlightthickness=1, highlightbackground="#dce3e6")
        panel.pack(anchor="nw", fill="x", pady=6)
        tk.Label(panel, text="Stevo POS", bg="white", fg="#163b40",
                 font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(panel, text="Sell simply. Keep your business moving.", bg="white", fg="#087e70",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(3, 14))
        tk.Label(panel, text="Stevo POS helps you serve customers, manage products and stock, "
                 "track orders, and review sales from one straightforward point-of-sale app. "
                 "Cash, M-Pesa, and card payments can be recorded at checkout.",
                 bg="white", fg="#53666b", justify="left", wraplength=700).pack(anchor="w")
        tk.Label(panel, text="Your data and files", bg="white", fg="#163b40",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(20, 6))
        tk.Label(panel, text=f"Sales, products, users, and settings are saved on this computer in the local database:\n"
                 f"{self.db.path}\n\nReceipts are saved as text files in the data/receipts folder. "
                 "Sales reports can be exported as CSV files to a location you choose. "
                 "Back up the database file and receipts folder to keep your records safe. "
                 "No payment provider is contacted by this app.",
                 bg="white", fg="#64777c", justify="left", wraplength=700).pack(anchor="w")
        tk.Label(panel, text="Developed and maintained by ASHTECK CO-OP", bg="white", fg="#163b40",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(22, 0))

    def change_password_dialog(self) -> None:
        password = simpledialog.askstring("Change password", "Enter a new password (at least 6 characters):", show="•", parent=self.root)
        if password:
            try:
                self.db.change_password(self.user["username"], password)
                messagebox.showinfo("Password updated", "Your password has been changed.", parent=self.root)
            except ValueError as error:
                messagebox.showerror("Password not changed", str(error), parent=self.root)

    def focus_search(self) -> None:
        if hasattr(self, "search_entry") and self.search_entry.winfo_exists():
            self.search_entry.focus_set()

    def refresh_current(self) -> None:
        if hasattr(self, "orders_tree") and self.orders_tree.winfo_exists():
            self.show_orders()
        elif hasattr(self, "product_admin_tree") and self.product_admin_tree.winfo_exists():
            self.show_products()
        elif hasattr(self, "product_tree") and self.product_tree.winfo_exists():
            self.populate_products()
            self.populate_basket()
        else:
            self.show_dashboard()

    def show_shortcuts(self, event: object = None) -> None:
        hotkeys = self.db.get_hotkeys()
        lines = [f"{display_hotkey(hotkeys[action])}  {title}" for action, title in HOTKEY_ACTIONS if hotkeys[action]]
        messagebox.showinfo("Keyboard shortcuts", "\n".join(lines) or "No shortcuts configured.", parent=self.root)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "--init-db" in sys.argv[1:]:
        PosDB(DB_PATH)
        return
    root = tk.Tk()
    PosApp(root, PosDB(DB_PATH))
    root.mainloop()


if __name__ == "__main__":
    main()
