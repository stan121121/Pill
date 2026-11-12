"""SQLite-хранилище: расчётные позиции и кошельки (stdlib)."""
import asyncio
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

DEFAULT_THRESHOLD = 1.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    threshold REAL NOT NULL DEFAULT 1.5
);
CREATE TABLE IF NOT EXISTS positions (          -- мониторинг по данным калькулятора
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    supply_ticker TEXT NOT NULL, borrow_ticker TEXT NOT NULL,
    supply_amount REAL NOT NULL, borrow_tokens REAL NOT NULL,
    supply_price REAL NOT NULL, borrow_price REAL NOT NULL,
    supply_live INTEGER NOT NULL, borrow_live INTEGER NOT NULL,
    max_ltv REAL NOT NULL, lt REAL NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    last_alert_ts REAL NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (            -- мониторинг реальных позиций on-chain
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    address TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    UNIQUE (user_id, address)
);
CREATE TABLE IF NOT EXISTS wallet_state (
    wallet_id INTEGER NOT NULL,
    chain TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    last_alert_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet_id, chain)
);
"""


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _tx(self, fn):
        with self._lock:
            try:
                res = fn(self._conn)
                self._conn.commit()
                return res
            except Exception:
                self._conn.rollback()
                raise

    async def tx(self, fn):
        return await asyncio.to_thread(self._tx, fn)

    # ---- настройки ----
    async def get_threshold(self, user_id: int) -> float:
        def f(c):
            r = c.execute("SELECT threshold FROM users WHERE user_id=?", (user_id,)).fetchone()
            return r["threshold"] if r else DEFAULT_THRESHOLD
        return await self.tx(f)

    async def set_threshold(self, user_id: int, value: float):
        await self.tx(lambda c: c.execute(
            "INSERT INTO users(user_id, threshold) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET threshold=excluded.threshold", (user_id, value)))

    async def remove_user(self, user_id: int):
        def f(c):
            c.execute("DELETE FROM wallet_state WHERE wallet_id IN (SELECT id FROM wallets WHERE user_id=?)", (user_id,))
            c.execute("DELETE FROM wallets WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM positions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await self.tx(f)

    # ---- расчётные позиции ----
    async def count_positions(self, user_id: int) -> int:
        return await self.tx(lambda c: c.execute(
            "SELECT COUNT(*) n FROM positions WHERE user_id=?", (user_id,)).fetchone()["n"])

    async def add_position(self, user_id: int, d: dict) -> int:
        def f(c):
            return c.execute(
                "INSERT INTO positions(user_id, address, supply_ticker, borrow_ticker, supply_amount, "
                "borrow_tokens, supply_price, borrow_price, supply_live, borrow_live, max_ltv, lt, created_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, d["address"], d["supply_ticker"], d["borrow_ticker"], d["supply_amount"],
                 d["borrow_tokens"], d["supply_price"], d["borrow_price"],
                 int(d["supply_live"]), int(d["borrow_live"]), d["max_ltv"], d["lt"], time.time()),
            ).lastrowid
        return await self.tx(f)

    async def list_positions(self, user_id: int) -> List[dict]:
        return await self.tx(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM positions WHERE user_id=? ORDER BY id", (user_id,)).fetchall()])

    async def all_positions(self) -> List[dict]:
        return await self.tx(lambda c: [dict(r) for r in c.execute(
            "SELECT p.*, COALESCE(u.threshold, ?) AS threshold "
            "FROM positions p LEFT JOIN users u ON u.user_id = p.user_id",
            (DEFAULT_THRESHOLD,)).fetchall()])

    async def remove_position(self, user_id: int, position_id: int) -> bool:
        return await self.tx(lambda c: c.execute(
            "DELETE FROM positions WHERE id=? AND user_id=?", (position_id, user_id)).rowcount > 0)

    async def set_state(self, position_id: int, level: int, ts: float):
        await self.tx(lambda c: c.execute(
            "UPDATE positions SET level=?, last_alert_ts=? WHERE id=?", (level, ts, position_id)))

    # ---- кошельки ----
    async def count_wallets(self, user_id: int) -> int:
        return await self.tx(lambda c: c.execute(
            "SELECT COUNT(*) n FROM wallets WHERE user_id=?", (user_id,)).fetchone()["n"])

    async def add_wallet(self, user_id: int, address: str, label: str = "") -> Optional[int]:
        """id нового кошелька или None, если такой уже есть."""
        def f(c):
            cur = c.execute("INSERT OR IGNORE INTO wallets(user_id, address, label) VALUES(?,?,?)",
                            (user_id, address, label))
            return cur.lastrowid if cur.rowcount > 0 else None
        return await self.tx(f)

    async def list_wallets(self, user_id: int) -> List[dict]:
        return await self.tx(lambda c: [dict(r) for r in c.execute(
            "SELECT id, address, label FROM wallets WHERE user_id=? ORDER BY id", (user_id,)).fetchall()])

    async def all_wallets(self) -> List[dict]:
        return await self.tx(lambda c: [dict(r) for r in c.execute(
            "SELECT w.id, w.user_id, w.address, w.label, COALESCE(u.threshold, ?) AS threshold "
            "FROM wallets w LEFT JOIN users u ON u.user_id = w.user_id",
            (DEFAULT_THRESHOLD,)).fetchall()])

    async def remove_wallet(self, user_id: int, wallet_id: int) -> bool:
        def f(c):
            ok = c.execute("DELETE FROM wallets WHERE id=? AND user_id=?", (wallet_id, user_id)).rowcount > 0
            if ok:
                c.execute("DELETE FROM wallet_state WHERE wallet_id=?", (wallet_id,))
            return ok
        return await self.tx(f)

    async def get_wallet_state(self, wallet_id: int, chain: str) -> Tuple[int, float]:
        def f(c):
            r = c.execute("SELECT level, last_alert_ts FROM wallet_state WHERE wallet_id=? AND chain=?",
                          (wallet_id, chain)).fetchone()
            return (r["level"], r["last_alert_ts"]) if r else (0, 0.0)
        return await self.tx(f)

    async def set_wallet_state(self, wallet_id: int, chain: str, level: int, ts: float):
        await self.tx(lambda c: c.execute(
            "INSERT INTO wallet_state(wallet_id, chain, level, last_alert_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(wallet_id, chain) DO UPDATE SET level=excluded.level, last_alert_ts=excluded.last_alert_ts",
            (wallet_id, chain, level, ts)))
