"""Чтение позиций Aave v3-совместимых пулов (eth_call, без ключей) во всех сетях из chains.py."""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Union

import aiohttp

from chains import CHAINS, Chain, enabled_chains

log = logging.getLogger("onchain")

SELECTOR = "0xbf92857c"  # getUserAccountData(address)
MAX_UINT256 = 2**256 - 1
PROBE_ADDRESS = "0x000000000000000000000000000000000000dEaD"
REVALIDATE_SEC = 600


class RpcError(Exception):
    pass


class RpcTransient(RpcError):
    """Сеть/лимиты/таймаут: повторить позже."""


class RpcRevert(RpcError):
    """Контракт отклонил вызов: адрес пула, скорее всего, неверный."""


class BadData(RpcError):
    """Ответ не похож на getUserAccountData."""


@dataclass(frozen=True)
class Position:
    chain: str
    collateral_usd: float
    debt_usd: float
    available_borrow_usd: float
    ltv: float             # доля (0.8 = 80%)
    liq_threshold: float   # доля
    health_factor: float   # inf, если долга нет

    @property
    def has_position(self) -> bool:
        return self.collateral_usd > 0 or self.debt_usd > 0

    @property
    def current_ltv(self) -> float:
        return self.debt_usd / self.collateral_usd if self.collateral_usd > 0 else 0.0

    @property
    def drop_to_liquidation(self) -> Optional[float]:
        """Падение стоимости залога (доля) до ликвидации при неизменном долге."""
        hf = self.health_factor
        if hf == float("inf"):
            return None
        if hf <= 1.0:
            return 0.0
        return 1 - 1 / hf


def encode_call(address: str) -> str:
    return SELECTOR + address[2:].lower().rjust(64, "0")


def decode_position(chain_key: str, result: str) -> Position:
    raw = result[2:] if result.startswith("0x") else result
    if len(raw) < 6 * 64:
        raise BadData(f"unexpected result: {result[:80]!r}")
    collateral, debt, available, liq_bps, ltv_bps, hf_raw = (
        int(raw[i * 64:(i + 1) * 64], 16) for i in range(6)
    )
    return Position(
        chain=chain_key,
        collateral_usd=collateral / 1e8,  # базовая валюта Aave v3 = USD, 8 знаков
        debt_usd=debt / 1e8,
        available_borrow_usd=available / 1e8,
        ltv=ltv_bps / 10_000,
        liq_threshold=liq_bps / 10_000,
        health_factor=float("inf") if hf_raw == MAX_UINT256 else hf_raw / 1e18,
    )


# ------------------------------------------------------------------ транспорт
_session: Optional[aiohttp.ClientSession] = None
_sem: Optional[asyncio.Semaphore] = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(20)
    return _sem


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    return _session


async def close():
    if _session and not _session.closed:
        await _session.close()


async def _rpc(chain: Chain, method: str, params: list):
    """JSON-RPC с перебором RPC-адресов сети. Ревёрт не ретраим, остальное пробуем на следующем RPC."""
    last: Exception = RpcTransient("нет RPC")
    for url in chain.rpcs:
        try:
            async with _get_sem():
                session = await _get_session()
                async with session.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            if "error" in body:
                msg = str(body["error"])[:200]
                raise (RpcRevert if "revert" in msg.lower() else RpcTransient)(msg)
            return body["result"]
        except RpcRevert:
            raise
        except Exception as e:  # noqa: BLE001
            last = e if isinstance(e, RpcError) else RpcTransient(str(e)[:200])
    raise last


# ------------------------------------------------------------------ самопроверка сетей
async def validate_chain(chain: Chain):
    chain.checked_ts = time.time()
    old = chain.status
    if not chain.pool:
        chain.status, chain.detail = "no_pool", f"укажите POOL_{chain.key.upper()}"
        return
    try:
        if chain.chain_id:
            cid = int(await _rpc(chain, "eth_chainId", []), 16)
            if cid != chain.chain_id:
                raise BadData(f"RPC вернул chainId {cid}, ожидался {chain.chain_id}")
        res = await _rpc(chain, "eth_call", [{"to": chain.pool, "data": encode_call(PROBE_ADDRESS)}, "latest"])
        if not res or res == "0x":
            raise BadData("по адресу пула нет контракта")
        decode_position(chain.key, res)
        chain.status, chain.detail = "ok", ""
    except (BadData, RpcRevert) as e:
        chain.status, chain.detail = "bad_config", str(e)[:120]
    except Exception as e:  # noqa: BLE001
        chain.status, chain.detail = "unreachable", str(e)[:120]
    if chain.status != old:
        log.info("chain %s: %s -> %s %s", chain.key, old, chain.status, chain.detail)


async def validate_all():
    await asyncio.gather(*(validate_chain(c) for c in CHAINS.values() if c.pool))


async def ensure_validated():
    """Проверяем непроверенные сети, а проблемные перепроверяем раз в REVALIDATE_SEC."""
    now = time.time()
    todo = [c for c in CHAINS.values() if c.pool and (
        c.status == "unknown" or (c.status in ("unreachable", "bad_config") and now - c.checked_ts > REVALIDATE_SEC))]
    if todo:
        await asyncio.gather(*(validate_chain(c) for c in todo))


# ------------------------------------------------------------------ позиции
async def fetch_position(chain: Chain, address: str) -> Position:
    res = await _rpc(chain, "eth_call", [{"to": chain.pool, "data": encode_call(address)}, "latest"])
    return decode_position(chain.key, res)


async def fetch_all(address: str, only: Optional[Iterable[str]] = None) -> Dict[str, Union[Position, Exception]]:
    """Опрос сетей параллельно. only=None: все доступные сети; иначе только перечисленные ключи.
    Ошибка сети возвращается как Exception в значении."""
    chains = enabled_chains()
    if only is not None:
        only = set(only)
        chains = [c for c in chains if c.key in only]
    results = await asyncio.gather(*(fetch_position(c, address) for c in chains), return_exceptions=True)
    return {c.key: r for c, r in zip(chains, results)}
