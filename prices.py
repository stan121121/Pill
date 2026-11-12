"""Цены CoinMarketCap: пакетные запросы + кэш."""
import logging
import time
from typing import Dict, Iterable, Optional, Tuple

import aiohttp

log = logging.getLogger("prices")


class CoinMarketCapPriceFetcher:
    BASE_URL = "https://pro-api.coinmarketcap.com/v2"

    def __init__(self, api_key: str = "", cache_ttl: int = 240):
        self._api_key = api_key
        self._ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (price, ts)
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_ok_ts: Optional[float] = None
        self._stats = {"api_calls": 0, "success": 0, "fail": 0, "cache_hits": 0}

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def get_stats(self) -> dict:
        return {**self._stats, "cache_size": len(self._cache)}

    async def get_prices(self, symbols: Iterable[str]) -> Dict[str, float]:
        """Возвращает цены только для тех тикеров, что удалось получить."""
        if not self.is_available():
            return {}
        symbols = {s.upper().strip() for s in symbols if s}
        now = time.time()
        result: Dict[str, float] = {}
        missing = []
        for s in symbols:
            hit = self._cache.get(s)
            if hit and now - hit[1] < self._ttl:
                result[s] = hit[0]
                self._stats["cache_hits"] += 1
            else:
                missing.append(s)
        if not missing:
            return result

        try:
            session = await self._get_session()
            self._stats["api_calls"] += 1
            async with session.get(
                f"{self.BASE_URL}/cryptocurrency/quotes/latest",
                headers={"X-CMC_PRO_API_KEY": self._api_key, "Accept": "application/json"},
                params={"symbol": ",".join(sorted(missing)), "skip_invalid": "true"},
            ) as resp:
                if resp.status != 200:
                    self._stats["fail"] += 1
                    log.warning("CMC status %s", resp.status)
                    return self._with_stale(result, missing)
                body = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            self._stats["fail"] += 1
            log.warning("CMC request failed: %s", e)
            return self._with_stale(result, missing)

        self._stats["success"] += 1
        self.last_ok_ts = now
        data = body.get("data") or {}
        for s in missing:
            price = self._extract(data.get(s))
            if price is not None:
                self._cache[s] = (price, now)
                result[s] = price
        return result

    @staticmethod
    def _extract(entry) -> Optional[float]:
        """v2 отдаёт список совпадений по символу: берём с лучшим рангом CMC."""
        if entry is None:
            return None
        entries = entry if isinstance(entry, list) else [entry]
        valid = [e for e in entries if ((e.get("quote") or {}).get("USD") or {}).get("price") is not None]
        if not valid:
            return None
        best = min(valid, key=lambda e: e.get("cmc_rank") or 10**9)
        return float(best["quote"]["USD"]["price"])

    def _with_stale(self, result: Dict[str, float], missing) -> Dict[str, float]:
        # при сбое API не отдаём устаревшие цены как свежие: мониторинг пропустит цикл
        return result

    async def get_price_usd(self, symbol: str) -> Optional[float]:
        return (await self.get_prices([symbol])).get(symbol.upper().strip())
