import sqlite3
import threading
import random
from datetime import datetime


class Database:
    def __init__(self, path="store.db"):
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path, check_same_thread=False)

    def _init(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    original_name TEXT NOT NULL,
                    branded_name TEXT NOT NULL,
                    status TEXT DEFAULT 'available',
                    sold_to_id INTEGER,
                    sold_to_username TEXT,
                    sold_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    buyer_id INTEGER,
                    buyer_username TEXT,
                    quantity INTEGER,
                    total_idr INTEGER,
                    total_usd REAL,
                    payment_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS buyers (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_seen TEXT
                )
            """)
            conn.commit()

    def add_file_local(self, file_path: str, original_name: str, branded_name: str) -> bool:
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO files (file_path, original_name, branded_name) VALUES (?,?,?)",
                    (file_path, original_name, branded_name)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # duplicate

    def get_stock_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='available'"
            ).fetchone()
            return row[0]

    def claim_files(self, quantity: int, buyer_id: int, buyer_username: str):
        """Returns list of (file_path, branded_name) tuples."""
        with self.lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, file_path, branded_name FROM files WHERE status='available' LIMIT ?",
                    (quantity,)
                ).fetchall()

                if len(rows) < quantity:
                    return []

                now = datetime.utcnow().isoformat()
                ids = [r[0] for r in rows]
                result = [(r[1], r[2]) for r in rows]

                conn.execute(
                    f"UPDATE files SET status='sold', sold_to_id=?, sold_to_username=?, sold_at=? "
                    f"WHERE id IN ({','.join('?'*len(ids))})",
                    [buyer_id, buyer_username, now] + ids
                )
                conn.execute(
                    "INSERT OR IGNORE INTO buyers (user_id, username, first_seen) VALUES (?,?,?)",
                    (buyer_id, buyer_username, now)
                )
                conn.commit()
                return result

    def get_random_file_for_promo(self):
        """Returns (file_path, branded_name) of a random available file."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT file_path, branded_name FROM files WHERE status='available'"
            ).fetchall()
            if not rows:
                return None
            return random.choice(rows)

    def create_pending_order(self, order_id, buyer_id, buyer_username, quantity, total_idr, total_usd):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO orders (order_id,buyer_id,buyer_username,quantity,total_idr,total_usd,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (order_id, buyer_id, buyer_username, quantity, total_idr, total_usd, datetime.utcnow().isoformat())
            )
            conn.commit()

    def set_order_payment_id(self, order_id, payment_id):
        with self._conn() as conn:
            conn.execute("UPDATE orders SET payment_id=? WHERE order_id=?", (payment_id, order_id))
            conn.commit()

    def get_order(self, order_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                return None
            cols = ["order_id","buyer_id","buyer_username","quantity","total_idr","total_usd","payment_id","status","created_at"]
            return dict(zip(cols, row))

    def mark_order_paid(self, order_id):
        with self._conn() as conn:
            conn.execute("UPDATE orders SET status='paid' WHERE order_id=?", (order_id,))
            conn.commit()

    def get_all_buyer_ids(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM buyers").fetchall()
            return [r[0] for r in rows]
