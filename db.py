"""Async SQLite database layer (V2).

All money/stock mutations run inside a single BEGIN IMMEDIATE transaction
guarded by an asyncio lock, so concurrent buyers can never overspend stock
or drive a balance negative.

Backends:
  - local SQLite via aiosqlite (default; used for dev and by the test suite)
  - Turso cloud DB via the libsql client, when TURSO_DATABASE_URL and
    TURSO_AUTH_TOKEN are set (Render free tier: ephemeral disk, so the
    database must live off-box). libsql is synchronous, so every call runs
    on one dedicated thread; the async surface mirrors aiosqlite exactly.
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    wallet_balance REAL NOT NULL DEFAULT 0,
    referral_earnings REAL NOT NULL DEFAULT 0,
    total_spent REAL NOT NULL DEFAULT 0,
    referred_by INTEGER,
    referral_rewarded INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    joined INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referee_id INTEGER NOT NULL UNIQUE,
    rewarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    how_to_use TEXT NOT NULL DEFAULT '',
    terms TEXT NOT NULL DEFAULT 'Digital product: no refund or replacement after delivery.',
    supplier_sku TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stock_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    serial TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    order_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stock_product ON stock_items(product_id, used);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid',
    api_key_id INTEGER,
    external_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    amount REAL NOT NULL,
    amount_bdt REAL,
    trxid TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    anonymous INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, product_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "usd_rate": "120",
    "min_deposit": "0.20",
    "max_deposit": "100",
    "bkash_number": "",
    "nagad_number": "",
    "rocket_number": "",
    "binance_user": "",
    "bybit_user": "",
    "cashout_bkash": "2",
    "cashout_nagad": "2",
    "cashout_rocket": "2",
    "force_join_link": "https://t.me/subsmaart",
    "low_stock_threshold": "10",
    "supplier_base_url": "",
    "supplier_api_key": "",
}


class InsufficientFunds(Exception):
    pass


class InsufficientStock(Exception):
    def __init__(self, available: int):
        super().__init__(f"Only {available} in stock")
        self.available = available


# ---------------- Turso (libsql) backend ----------------
class _TursoCursor:
    """Async wrapper around a synchronous libsql cursor."""

    def __init__(self, cursor, loop: asyncio.AbstractEventLoop,
                 executor: ThreadPoolExecutor):
        self._cursor = cursor
        self._loop = loop
        self._executor = executor
        # lastrowid/rowcount are set synchronously by execute(); capture now.
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    async def fetchone(self):
        return await self._loop.run_in_executor(self._executor, self._cursor.fetchone)

    async def fetchall(self):
        return await self._loop.run_in_executor(self._executor, self._cursor.fetchall)


class _TursoConnection:
    """Async wrapper around the synchronous libsql client (Turso remote).

    All libsql calls run on a single dedicated thread so the sync
    connection is never touched from multiple threads. The async surface
    mirrors aiosqlite.Connection: execute / executemany / executescript /
    commit / rollback / close.
    """

    def __init__(self, url: str, auth_token: str):
        import libsql  # deferred import: only needed for the Turso backend

        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso-db")
        self._conn = libsql.connect(url, auth_token=auth_token)

    async def _call(self, func, *args):
        return await self._loop.run_in_executor(self._executor, func, *args)

    async def execute(self, sql, parameters=()):
        cursor = await self._call(self._conn.execute, sql, parameters)
        return _TursoCursor(cursor, self._loop, self._executor)

    async def executemany(self, sql, seq_of_parameters):
        cursor = await self._call(self._conn.executemany, sql, seq_of_parameters)
        return _TursoCursor(cursor, self._loop, self._executor)

    async def executescript(self, sql):
        await self._call(self._conn.executescript, sql)

    async def commit(self):
        await self._call(self._conn.commit)

    async def rollback(self):
        await self._call(self._conn.rollback)

    async def close(self):
        try:
            await self._call(self._conn.close)
        finally:
            self._executor.shutdown(wait=True)


class Database:
    def __init__(self, path: str, turso_url: str | None = None,
                 turso_token: str | None = None):
        self.path = path
        self.turso_url = (turso_url or "").strip()
        self.turso_token = (turso_token or "").strip()
        self._lock = asyncio.Lock()
        self.db = None  # aiosqlite.Connection | _TursoConnection | None

    @property
    def uses_turso(self) -> bool:
        """True when the Turso cloud backend is configured."""
        return bool(self.turso_url and self.turso_token)

    async def init(self) -> None:
        if self.uses_turso:
            self.db = _TursoConnection(self.turso_url, self.turso_token)
        else:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self.db = await aiosqlite.connect(self.path)
            await self.db.execute("PRAGMA journal_mode=WAL;")
            await self.db.execute("PRAGMA foreign_keys=ON;")
        await self.db.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    # ---------------- settings ----------------
    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._lock:
            cur = await self.db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await self.db.commit()

    async def all_settings(self) -> dict:
        async with self._lock:
            cur = await self.db.execute("SELECT key, value FROM settings ORDER BY key")
            return {r[0]: r[1] for r in await cur.fetchall()}

    # ---------------- users ----------------
    async def ensure_user(self, user_id: int, username: str | None) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO users (id, username) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
                (user_id, username or ""),
            )
            await self.db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, username, wallet_balance, referral_earnings, total_spent, "
                "referred_by, referral_rewarded, banned, joined, created_at "
                "FROM users WHERE id=?", (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "username": row[1], "wallet_balance": row[2],
                "referral_earnings": row[3], "total_spent": row[4], "referred_by": row[5],
                "referral_rewarded": bool(row[6]), "banned": bool(row[7]),
                "joined": bool(row[8]), "created_at": row[9],
            }

    async def get_balance(self, user_id: int) -> float:
        u = await self.get_user(user_id)
        return float(u["wallet_balance"]) if u else 0.0

    async def add_balance(self, user_id: int, amount: float) -> float:
        """Atomically adjust balance, floored at 0. Returns new balance."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "INSERT INTO users (id, wallet_balance) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET wallet_balance = "
                    "MAX(0, wallet_balance + excluded.wallet_balance)",
                    (user_id, amount),
                )
                cur = await self.db.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,))
                new_balance = round(float((await cur.fetchone())[0]), 2)
                await self.db.execute(
                    "UPDATE users SET wallet_balance=? WHERE id=?", (new_balance, user_id)
                )
                await self.db.commit()
                return new_balance
            except Exception:
                await self.db.rollback()
                raise

    async def set_banned(self, user_id: int, banned: bool) -> None:
        async with self._lock:
            await self.db.execute("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, user_id))
            await self.db.commit()

    async def set_referred_by(self, user_id: int, referrer_id: int) -> bool:
        """Record referrer once. Returns True if newly set."""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT referred_by FROM users WHERE id=?", (user_id,)
            )
            row = await cur.fetchone()
            if not row or row[0]:
                return False
            await self.db.execute(
                "UPDATE users SET referred_by=? WHERE id=?", (referrer_id, user_id)
            )
            await self.db.commit()
            return True

    async def reward_referral(self, referee_id: int, amount: float = 0.01) -> int | None:
        """Credit the referrer once. Returns referrer_id if a reward was paid."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    "SELECT referred_by, referral_rewarded FROM users WHERE id=?", (referee_id,)
                )
                row = await cur.fetchone()
                if not row or not row[0] or row[1]:
                    await self.db.rollback()
                    return None
                referrer_id = row[0]
                if referrer_id == referee_id:
                    await self.db.rollback()
                    return None
                cur = await self.db.execute("SELECT id FROM users WHERE id=?", (referrer_id,))
                if not await cur.fetchone():
                    await self.db.rollback()
                    return None
                cur = await self.db.execute(
                    "SELECT id FROM referrals WHERE referee_id=?", (referee_id,)
                )
                if await cur.fetchone():
                    await self.db.rollback()
                    return None
                await self.db.execute(
                    "INSERT INTO referrals (referrer_id, referee_id, rewarded) VALUES (?, ?, 1)",
                    (referrer_id, referee_id),
                )
                await self.db.execute(
                    "UPDATE users SET wallet_balance = wallet_balance + ?, "
                    "referral_earnings = referral_earnings + ? WHERE id=?",
                    (amount, amount, referrer_id),
                )
                await self.db.execute(
                    "UPDATE users SET referral_rewarded=1, joined=1 WHERE id=?", (referee_id,)
                )
                await self.db.commit()
                return referrer_id
            except Exception:
                await self.db.rollback()
                raise

    async def mark_joined(self, user_id: int) -> None:
        async with self._lock:
            await self.db.execute("UPDATE users SET joined=1 WHERE id=?", (user_id,))
            await self.db.commit()

    async def referral_stats(self, user_id: int) -> dict:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COUNT(*), SUM(rewarded) FROM referrals WHERE referrer_id=?", (user_id,)
            )
            row = await cur.fetchone()
            total = row[0] or 0
            rewarded = row[1] or 0
            cur = await self.db.execute(
                "SELECT referral_earnings FROM users WHERE id=?", (user_id,)
            )
            erow = await cur.fetchone()
            return {"invited": total, "rewarded": rewarded,
                    "earnings": round(float(erow[0]) if erow else 0.0, 2)}

    async def list_users(self, limit: int = 10, offset: int = 0) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, username, wallet_balance, total_spent, banned, created_at "
                "FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
            return [
                {"id": r[0], "username": r[1], "wallet_balance": r[2],
                 "total_spent": r[3], "banned": bool(r[4]), "created_at": r[5]}
                for r in await cur.fetchall()
            ]

    async def count_users(self) -> int:
        async with self._lock:
            cur = await self.db.execute("SELECT COUNT(*) FROM users")
            return int((await cur.fetchone())[0])

    async def search_users(self, query: str) -> list[dict]:
        async with self._lock:
            like = f"%{query.lstrip('@')}%"
            params: tuple
            if query.strip().isdigit():
                cur = await self.db.execute(
                    "SELECT id, username, wallet_balance, total_spent, banned FROM users "
                    "WHERE id=? OR username LIKE ? LIMIT 10", (int(query.strip()), like)
                )
            else:
                cur = await self.db.execute(
                    "SELECT id, username, wallet_balance, total_spent, banned FROM users "
                    "WHERE username LIKE ? LIMIT 10", (like,)
                )
            return [
                {"id": r[0], "username": r[1], "wallet_balance": r[2],
                 "total_spent": r[3], "banned": bool(r[4])}
                for r in await cur.fetchall()
            ]

    async def all_user_ids(self) -> list[int]:
        async with self._lock:
            cur = await self.db.execute("SELECT id FROM users WHERE banned=0")
            return [r[0] for r in await cur.fetchall()]

    # ---------------- products ----------------
    async def add_product(self, name: str, price: float, description: str,
                          how_to_use: str, terms: str, supplier_sku: str = "") -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO products (name, price, description, how_to_use, terms, supplier_sku) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, price, description, how_to_use, terms, supplier_sku or None),
            )
            await self.db.commit()
            return cur.lastrowid

    async def update_product(self, product_id: int, **fields) -> None:
        allowed = {"name", "price", "description", "how_to_use", "terms", "supplier_sku", "active"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        async with self._lock:
            sets = ", ".join(f"{k}=?" for k in updates)
            await self.db.execute(
                f"UPDATE products SET {sets} WHERE id=?", (*updates.values(), product_id)
            )
            await self.db.commit()

    async def delete_product(self, product_id: int) -> None:
        async with self._lock:
            await self.db.execute("DELETE FROM stock_items WHERE product_id=?", (product_id,))
            await self.db.execute("DELETE FROM products WHERE id=?", (product_id,))
            await self.db.commit()

    async def get_product(self, product_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, name, price, description, how_to_use, terms, supplier_sku, active "
                "FROM products WHERE id=?", (product_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "name": row[1], "price": row[2], "description": row[3],
                    "how_to_use": row[4], "terms": row[5], "supplier_sku": row[6],
                    "active": bool(row[7])}

    async def list_products(self, active_only: bool = True) -> list[dict]:
        async with self._lock:
            q = "SELECT id, name, price, active FROM products"
            if active_only:
                q += " WHERE active=1"
            q += " ORDER BY id"
            cur = await self.db.execute(q)
            return [{"id": r[0], "name": r[1], "price": r[2], "active": bool(r[3])}
                    for r in await cur.fetchall()]

    async def list_products_with_stock(self, active_only: bool = True) -> list[dict]:
        async with self._lock:
            q = ("SELECT p.id, p.name, p.price, p.active, "
                 "(SELECT COUNT(*) FROM stock_items s WHERE s.product_id=p.id AND s.used=0) "
                 "FROM products p")
            if active_only:
                q += " WHERE p.active=1"
            q += " ORDER BY p.id"
            cur = await self.db.execute(q)
            return [{"id": r[0], "name": r[1], "price": r[2], "active": bool(r[3]), "stock": r[4]}
                    for r in await cur.fetchall()]

    # ---------------- stock ----------------
    async def count_stock(self, product_id: int) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COUNT(*) FROM stock_items WHERE product_id=? AND used=0", (product_id,)
            )
            return int((await cur.fetchone())[0])

    async def add_stock(self, product_id: int, serials: list[str]) -> int:
        clean, seen = [], set()
        for s in serials:
            s = s.strip()
            if s and s not in seen:
                seen.add(s)
                clean.append(s)
        if not clean:
            return 0
        async with self._lock:
            await self.db.executemany(
                "INSERT INTO stock_items (product_id, serial) VALUES (?, ?)",
                [(product_id, s) for s in clean],
            )
            await self.db.commit()
            return len(clean)

    async def clear_stock(self, product_id: int) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "DELETE FROM stock_items WHERE product_id=? AND used=0", (product_id,)
            )
            await self.db.commit()
            return cur.rowcount

    async def get_order_serials(self, order_id: int) -> list[str]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT serial FROM stock_items WHERE order_id=? ORDER BY id", (order_id,)
            )
            return [r[0] for r in await cur.fetchall()]

    # ---------------- orders ----------------
    async def purchase(self, user_id: int, product_id: int, qty: int):
        """Atomic purchase: funds + stock check, deduct, reserve, create order.
        Returns (order_id, serials, new_balance, remaining_stock)."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,))
                row = await cur.fetchone()
                balance = float(row[0]) if row else 0.0
                cur = await self.db.execute(
                    "SELECT price, active FROM products WHERE id=?", (product_id,)
                )
                prod = await cur.fetchone()
                if not prod or not prod[1]:
                    await self.db.rollback()
                    raise InsufficientStock(0)
                total = round(float(prod[0]) * qty, 2)
                if balance + 1e-9 < total:
                    await self.db.rollback()
                    raise InsufficientFunds()
                cur = await self.db.execute(
                    "SELECT id, serial FROM stock_items WHERE product_id=? AND used=0 "
                    "ORDER BY id LIMIT ?", (product_id, qty)
                )
                items = await cur.fetchall()
                if len(items) < qty:
                    await self.db.rollback()
                    raise InsufficientStock(len(items))
                new_balance = round(balance - total, 2)
                await self.db.execute(
                    "UPDATE users SET wallet_balance=?, total_spent=total_spent+? WHERE id=?",
                    (new_balance, total, user_id),
                )
                cur = await self.db.execute(
                    "INSERT INTO orders (user_id, product_id, qty, total, status) "
                    "VALUES (?, ?, ?, ?, 'paid')", (user_id, product_id, qty, total)
                )
                order_id = cur.lastrowid
                ids = [r[0] for r in items]
                ph = ",".join("?" for _ in ids)
                await self.db.execute(
                    f"UPDATE stock_items SET used=1, order_id=? WHERE id IN ({ph})",
                    (order_id, *ids),
                )
                cur = await self.db.execute(
                    "SELECT COUNT(*) FROM stock_items WHERE product_id=? AND used=0",
                    (product_id,),
                )
                remaining = int((await cur.fetchone())[0])
                await self.db.commit()
                return order_id, [r[1] for r in items], new_balance, remaining
            except Exception:
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                raise

    async def api_place_order(self, api_key_id: int, product_id: int, qty: int,
                              external_ref: str | None = None):
        """Atomic stock reservation for supplier-API orders (no wallet involved)."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    "SELECT price, active FROM products WHERE id=?", (product_id,)
                )
                prod = await cur.fetchone()
                if not prod or not prod[1]:
                    await self.db.rollback()
                    raise InsufficientStock(0)
                total = round(float(prod[0]) * qty, 2)
                cur = await self.db.execute(
                    "SELECT id, serial FROM stock_items WHERE product_id=? AND used=0 "
                    "ORDER BY id LIMIT ?", (product_id, qty)
                )
                items = await cur.fetchall()
                if len(items) < qty:
                    await self.db.rollback()
                    raise InsufficientStock(len(items))
                cur = await self.db.execute(
                    "INSERT INTO orders (user_id, product_id, qty, total, status, api_key_id, external_ref) "
                    "VALUES (0, ?, ?, ?, 'paid', ?, ?)",
                    (product_id, qty, total, api_key_id, external_ref),
                )
                order_id = cur.lastrowid
                ids = [r[0] for r in items]
                ph = ",".join("?" for _ in ids)
                await self.db.execute(
                    f"UPDATE stock_items SET used=1, order_id=? WHERE id IN ({ph})",
                    (order_id, *ids),
                )
                await self.db.commit()
                return order_id, [r[1] for r in items], total
            except Exception:
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                raise

    async def list_orders(self, user_id: int, limit: int = 20) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT o.id, p.name, o.qty, o.total, o.status, o.created_at "
                "FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.user_id=? ORDER BY o.id DESC LIMIT ?", (user_id, limit)
            )
            return [{"id": r[0], "product": r[1], "qty": r[2], "total": r[3],
                     "status": r[4], "created_at": r[5]} for r in await cur.fetchall()]

    async def get_order(self, order_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT o.id, o.user_id, o.product_id, p.name, o.qty, o.total, o.status, o.created_at, "
                "o.api_key_id, o.external_ref "
                "FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?", (order_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "user_id": row[1], "product_id": row[2], "product": row[3],
                    "qty": row[4], "total": row[5], "status": row[6], "created_at": row[7],
                    "api_key_id": row[8], "external_ref": row[9]}

    async def recent_orders(self, limit: int = 10) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT o.id, o.user_id, u.username, p.name, o.qty, o.total, o.created_at "
                "FROM orders o JOIN products p ON p.id=o.product_id "
                "LEFT JOIN users u ON u.id=o.user_id "
                "ORDER BY o.id DESC LIMIT ?", (limit,)
            )
            return [{"id": r[0], "user_id": r[1], "username": r[2], "product": r[3],
                     "qty": r[4], "total": r[5], "created_at": r[6]}
                    for r in await cur.fetchall()]

    async def shop_stats(self) -> dict:
        async with self._lock:
            cur = await self.db.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders")
            o = await cur.fetchone()
            cur = await self.db.execute("SELECT COUNT(*) FROM users")
            u = (await cur.fetchone())[0]
            cur = await self.db.execute("SELECT COUNT(*) FROM users WHERE banned=1")
            b = (await cur.fetchone())[0]
            cur = await self.db.execute("SELECT COUNT(*) FROM deposits WHERE status IN ('pending','pending_auto')")
            d = (await cur.fetchone())[0]
            cur = await self.db.execute("SELECT COUNT(*) FROM stock_items WHERE used=0")
            s = (await cur.fetchone())[0]
            return {"orders": o[0], "revenue": round(o[1], 2), "users": u,
                    "banned": b, "pending_deposits": d, "stock_units": s}

    # ---------------- deposits ----------------
    async def create_deposit(self, user_id: int, method: str, amount: float,
                             amount_bdt: float | None, trxid: str | None,
                             status: str = "pending") -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO deposits (user_id, method, amount, amount_bdt, trxid, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, method, amount, amount_bdt, trxid, status),
            )
            await self.db.commit()
            return cur.lastrowid

    async def trxid_exists(self, trxid: str) -> bool:
        if not trxid:
            return False
        async with self._lock:
            cur = await self.db.execute(
                "SELECT 1 FROM deposits WHERE trxid=? AND status != 'rejected' LIMIT 1",
                (trxid.strip(),),
            )
            return bool(await cur.fetchone())

    async def list_pending_deposits(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT d.id, d.user_id, u.username, d.method, d.amount, d.amount_bdt, "
                "d.trxid, d.status, d.created_at "
                "FROM deposits d LEFT JOIN users u ON u.id=d.user_id "
                "WHERE d.status='pending' ORDER BY d.id"
            )
            return [{"id": r[0], "user_id": r[1], "username": r[2], "method": r[3],
                     "amount": r[4], "amount_bdt": r[5], "trxid": r[6],
                     "status": r[7], "created_at": r[8]} for r in await cur.fetchall()]

    async def user_deposits(self, user_id: int, limit: int = 15) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, method, amount, trxid, status, created_at FROM deposits "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
            )
            return [{"id": r[0], "method": r[1], "amount": r[2], "trxid": r[3],
                     "status": r[4], "created_at": r[5]} for r in await cur.fetchall()]

    async def approve_deposit(self, deposit_id: int) -> dict | None:
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    "SELECT user_id, method, amount, trxid, status FROM deposits WHERE id=?",
                    (deposit_id,),
                )
                row = await cur.fetchone()
                if not row or row[4] != "pending":
                    await self.db.rollback()
                    return None
                user_id, method, amount = row[0], row[1], row[2]
                await self.db.execute(
                    "UPDATE deposits SET status='approved' WHERE id=?", (deposit_id,)
                )
                await self.db.execute(
                    "INSERT INTO users (id, wallet_balance) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET wallet_balance = wallet_balance + excluded.wallet_balance",
                    (user_id, amount),
                )
                cur = await self.db.execute(
                    "SELECT wallet_balance FROM users WHERE id=?", (user_id,)
                )
                new_balance = round(float((await cur.fetchone())[0]), 2)
                await self.db.commit()
                return {"id": deposit_id, "user_id": user_id, "method": method,
                        "amount": amount, "new_balance": new_balance}
            except Exception:
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                raise

    async def reject_deposit(self, deposit_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE deposits SET status='rejected' WHERE id=? AND status='pending'",
                (deposit_id,),
            )
            await self.db.commit()
            if cur.rowcount == 0:
                return None
            cur = await self.db.execute(
                "SELECT user_id, method, amount FROM deposits WHERE id=?", (deposit_id,)
            )
            row = await cur.fetchone()
            return {"id": deposit_id, "user_id": row[0], "method": row[1], "amount": row[2]}

    # ---------------- reviews ----------------
    async def has_purchased(self, user_id: int, product_id: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT 1 FROM orders WHERE user_id=? AND product_id=? AND status='paid' LIMIT 1",
                (user_id, product_id),
            )
            return bool(await cur.fetchone())

    async def has_reviewed(self, user_id: int, product_id: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT 1 FROM reviews WHERE user_id=? AND product_id=? LIMIT 1",
                (user_id, product_id),
            )
            return bool(await cur.fetchone())

    async def add_review(self, user_id: int, product_id: int, stars: int,
                         comment: str, anonymous: bool) -> int | None:
        async with self._lock:
            try:
                cur = await self.db.execute(
                    "INSERT INTO reviews (user_id, product_id, stars, comment, anonymous) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, product_id, stars, comment, 1 if anonymous else 0),
                )
                await self.db.commit()
                return cur.lastrowid
            except Exception:
                await self.db.rollback()
                return None

    async def approved_reviews(self, product_id: int, limit: int = 5) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT r.stars, r.comment, r.anonymous, u.username, r.created_at "
                "FROM reviews r LEFT JOIN users u ON u.id=r.user_id "
                "WHERE r.product_id=? AND r.status='approved' "
                "ORDER BY r.id DESC LIMIT ?", (product_id, limit)
            )
            return [{"stars": r[0], "comment": r[1], "anonymous": bool(r[2]),
                     "username": r[3], "created_at": r[4]} for r in await cur.fetchall()]

    async def avg_stars(self, product_id: int) -> tuple[float, int]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT AVG(stars), COUNT(*) FROM reviews WHERE product_id=? AND status='approved'",
                (product_id,),
            )
            row = await cur.fetchone()
            return (round(row[0], 1) if row[0] else 0.0, row[1])

    async def pending_reviews(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT r.id, r.user_id, u.username, p.name, r.stars, r.comment, r.created_at "
                "FROM reviews r JOIN products p ON p.id=r.product_id "
                "LEFT JOIN users u ON u.id=r.user_id "
                "WHERE r.status='pending' ORDER BY r.id"
            )
            return [{"id": r[0], "user_id": r[1], "username": r[2], "product": r[3],
                     "stars": r[4], "comment": r[5], "created_at": r[6]}
                    for r in await cur.fetchall()]

    async def set_review_status(self, review_id: int, status: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE reviews SET status=? WHERE id=? AND status='pending'",
                (status, review_id),
            )
            await self.db.commit()
            return cur.rowcount > 0

    # ---------------- API keys ----------------
    async def create_api_key(self, name: str, key_hash: str, key_prefix: str) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix) VALUES (?, ?, ?)",
                (name, key_hash, key_prefix),
            )
            await self.db.commit()
            return cur.lastrowid

    async def list_api_keys(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, name, key_prefix, active, created_at FROM api_keys ORDER BY id DESC"
            )
            return [{"id": r[0], "name": r[1], "key_prefix": r[2],
                     "active": bool(r[3]), "created_at": r[4]} for r in await cur.fetchall()]

    async def revoke_api_key(self, key_id: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE api_keys SET active=0 WHERE id=?", (key_id,)
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def verify_api_key(self, key_hash: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, name FROM api_keys WHERE key_hash=? AND active=1", (key_hash,)
            )
            row = await cur.fetchone()
            return {"id": row[0], "name": row[1]} if row else None
