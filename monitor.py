"""Два фоновых воркера: расчётные позиции (цены CMC) и реальные кошельки (on-chain)."""
import asyncio
import logging
import os
import time
from typing import Dict, Set, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

import calc
import onchain
from db import DB
from prices import CoinMarketCapPriceFetcher

log = logging.getLogger("monitor")

CALC_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))        # расчётные позиции, сек
CHAIN_INTERVAL = int(os.getenv("CHAIN_INTERVAL", "180"))       # реальные кошельки, сек
FULL_SCAN_SEC = int(os.getenv("FULL_SCAN_MIN", "30")) * 60     # полный обход всех сетей
REMIND_WARN = int(os.getenv("REMIND_WARN_MIN", "60")) * 60
REMIND_CRIT = int(os.getenv("REMIND_CRIT_MIN", "15")) * 60
HYST = 1.03
CONCURRENCY = 10

last_chain_cycle: float = 0.0


def critical_level(threshold: float) -> float:
    """Красная зона: треть расстояния от 1.0 до порога (порог 1.5 → 1.165)."""
    return 1 + (threshold - 1) * 0.33


def classify(hf: float, threshold: float, prev: int) -> int:
    """0 = норма, 1 = ниже порога, 2 = критично; с гистерезисом."""
    crit = critical_level(threshold)
    lvl = 2 if hf < crit else 1 if hf < threshold else 0
    if lvl < prev:
        bound = crit if prev == 2 else threshold
        if hf < bound * HYST:
            lvl = prev
    return lvl


def decide(hf: float, thr: float, prev: int, prev_ts: float, now: float):
    """-> (вид уведомления | None, новый уровень)."""
    lvl = classify(hf, thr, prev)
    if lvl > prev:
        return ("crit" if lvl == 2 else "warn"), lvl
    if lvl == prev and lvl > 0 and now - prev_ts >= (REMIND_CRIT if lvl == 2 else REMIND_WARN):
        return ("remind_crit" if lvl == 2 else "remind_warn"), lvl
    if lvl < prev:
        return ("recovered" if lvl == 0 else "improved"), lvl
    return None, lvl


async def _send(bot: Bot, db: DB, user_id: int, text: str) -> bool:
    for _ in range(2):
        try:
            await bot.send_message(user_id, text)
            return True
        except TelegramForbiddenError:
            log.info("user %s blocked the bot, removing", user_id)
            await db.remove_user(user_id)
            return False
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:  # noqa: BLE001
            log.exception("send failed to %s", user_id)
            return False
    return False


# ====================================================================== расчётные позиции
async def _process_position(bot: Bot, db: DB, row: dict, c: dict):
    now = time.time()
    kind, lvl = decide(c["hf"], row["threshold"], row["level"], row["last_alert_ts"], now)
    if kind and await _send(bot, db, row["user_id"], calc.alert_text(kind, row, c, row["threshold"])):
        await db.set_state(row["id"], lvl, now if lvl > 0 else 0)


async def positions_cycle(bot: Bot, db: DB, cmc: CoinMarketCapPriceFetcher):
    rows = await db.all_positions()
    if not rows:
        return
    symbols = calc.symbols_needed(rows)
    prices = await cmc.get_prices(symbols) if symbols else {}
    skipped = 0
    for row in rows:
        pr = calc.row_prices(row, prices)
        if pr is None:
            skipped += 1  # нет свежей цены: состояние не трогаем
            continue
        try:
            await _process_position(bot, db, row, calc.calc_for_row(row, *pr))
        except Exception:  # noqa: BLE001
            log.exception("position %s failed", row["id"])
    if skipped:
        log.warning("positions: skipped %s/%s, no fresh prices", skipped, len(rows))


# ====================================================================== реальные кошельки
# address -> (сети, где есть позиция или была ошибка, время последнего полного обхода)
_watch: Dict[str, Tuple[Set[str], float]] = {}


async def scan(address: str) -> dict:
    """Полный обход всех сетей раз в FULL_SCAN_SEC, между ними опрашиваем только «активные» сети."""
    now = time.time()
    entry = _watch.get(address)
    full = entry is None or now - entry[1] >= FULL_SCAN_SEC
    results = await onchain.fetch_all(address, only=None if full else entry[0])
    active = {k for k, r in results.items() if isinstance(r, Exception) or r.has_position}
    _watch[address] = (active, now if full else entry[1])
    return results


async def _process_wallet(bot: Bot, db: DB, row: dict, chain: str, pos):
    wid, thr = row["id"], row["threshold"]
    prev, prev_ts = await db.get_wallet_state(wid, chain)
    now = time.time()
    if pos.debt_usd <= 0:
        kind, lvl = ("closed", 0) if prev > 0 else (None, 0)
    else:
        kind, lvl = decide(pos.health_factor, thr, prev, prev_ts, now)
    if not kind:
        return
    title = f"{calc.wallet_title(row['address'], row['label'])}"
    if await _send(bot, db, row["user_id"], calc.wallet_alert_text(kind, title, pos, thr)):
        await db.set_wallet_state(wid, chain, lvl, now if lvl > 0 else 0)


async def wallets_cycle(bot: Bot, db: DB):
    global last_chain_cycle
    await onchain.ensure_validated()
    rows = await db.all_wallets()
    if not rows:
        return
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(addr: str):
        async with sem:
            return addr, await scan(addr)

    fetched = dict(await asyncio.gather(*(one(a) for a in {r["address"] for r in rows})))
    for row in rows:
        for chain, pos in fetched[row["address"]].items():
            if isinstance(pos, Exception):
                continue  # временная ошибка RPC: состояние не трогаем
            try:
                await _process_wallet(bot, db, row, chain, pos)
            except Exception:  # noqa: BLE001
                log.exception("wallet %s chain %s failed", row["id"], chain)
    last_chain_cycle = time.time()


# ====================================================================== циклы
async def _loop(name: str, interval: int, fn):
    log.info("%s monitor started, interval=%ss", name, interval)
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s cycle failed", name)
        await asyncio.sleep(interval)


async def monitor_loops(bot: Bot, db: DB, cmc: CoinMarketCapPriceFetcher):
    await asyncio.gather(
        _loop("positions", CALC_INTERVAL, lambda: positions_cycle(bot, db, cmc)),
        _loop("wallets", CHAIN_INTERVAL, lambda: wallets_cycle(bot, db)),
    )
