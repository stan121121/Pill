"""Реестр сетей с Aave v3-совместимыми пулами.

Пул читается методом getUserAccountData, поэтому подходят и форки Aave v3 (например, Spark).
Любую сеть можно добавить через EXTRA_CHAINS (JSON) без правки кода.
При старте каждый пул проходит самопроверку (см. onchain.validate_chain), поэтому
ошибочный адрес отключит сеть, а не даст ложное «позиций нет».
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger("chains")

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CANONICAL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"  # один адрес на Arbitrum/Optimism/Polygon/Avalanche


@dataclass
class Chain:
    key: str
    name: str
    emoji: str
    chain_id: Optional[int]
    pool: str
    rpcs: List[str]
    verified: bool = False   # адрес сверен с публичным реестром Aave
    status: str = "unknown"  # unknown | ok | unreachable | bad_config | no_pool
    detail: str = ""
    checked_ts: float = 0.0


def _c(key, name, emoji, chain_id, pool, rpcs, verified=False) -> Chain:
    return Chain(key, name, emoji, chain_id, pool, list(rpcs), verified)


_DEFAULTS = [
    _c("ethereum", "Ethereum", "🔷", 1, "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
       ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"], True),
    _c("ethereum_prime", "Ethereum Prime", "💠", 1, "0x4e033931ad43597d96D6bcc25c280717730B58B1",
       ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"]),
    _c("arbitrum", "Arbitrum", "🔵", 42161, CANONICAL,
       ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"], True),
    _c("base", "Base", "🟦", 8453, "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
       ["https://base-rpc.publicnode.com", "https://mainnet.base.org"], True),
    _c("optimism", "Optimism", "🔴", 10, CANONICAL,
       ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"], True),
    _c("polygon", "Polygon", "🟣", 137, CANONICAL,
       ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"], True),
    _c("avalanche", "Avalanche", "🔺", 43114, CANONICAL,
       ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"], True),
    # Monad: chain id и RPC подтверждены, адрес пула берите из адресной книги Aave (AaveV3Monad.POOL)
    _c("monad", "Monad", "🟪", 143, "", ["https://rpc.monad.xyz"]),
    _c("bnb", "BNB Chain", "🟡", 56, "0x6807dc923806fE8Fd134338EABCA509979a7e0cB",
       ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"]),
    _c("gnosis", "Gnosis", "🟢", 100, "0xb50201558B00496A145fE76f7424749556E326D8",
       ["https://gnosis-rpc.publicnode.com", "https://rpc.gnosischain.com"]),
    _c("scroll", "Scroll", "📜", 534352, "0x11fCfe756c05AD438e312a7fd934381537D3cFfe",
       ["https://scroll-rpc.publicnode.com", "https://rpc.scroll.io"]),
    _c("linea", "Linea", "⬛", 59144, "0xc47b8C00b0f69a36fa203Ffeac0334874574a8Ac",
       ["https://linea-rpc.publicnode.com", "https://rpc.linea.build"]),
    _c("celo", "Celo", "💛", 42220, "0x3E59A31363E2ad014dcbc521c4a0d5757d9f3402",
       ["https://celo-rpc.publicnode.com", "https://forno.celo.org"]),
    _c("sonic", "Sonic", "🎵", 146, "0x5362dBb1e601abF3a4c14c22ffEdA64042E5eAA3",
       ["https://sonic-rpc.publicnode.com", "https://rpc.soniclabs.com"]),
    _c("mantle", "Mantle", "🌿", 5000, "0x458F293454fE0d67EC0655f3672301301DD51422",
       ["https://mantle-rpc.publicnode.com", "https://rpc.mantle.xyz"]),
    _c("plasma", "Plasma", "⚡", 9745, "0x925a2A7214Ed92428B5b1B090F80b25700095e12",
       ["https://rpc.plasma.to"]),
    _c("zksync", "ZKsync Era", "🔲", 324, "0x78e30497a3c7527d953c6B1E3541b021A98Ac43c",
       ["https://mainnet.era.zksync.io"]),
    _c("metis", "Metis", "🔶", 1088, "0x90df02551bB792286e8D4f13E0e357b4Bf1D6a57",
       ["https://andromeda.metis.io/?owner=1088"]),
    _c("soneium", "Soneium", "🌅", 1868, "0xDd3d7A7d03D9fD9ef45f3E587287922eF65CA38B",
       ["https://rpc.soneium.org"]),
]


def _split(v) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _build() -> Dict[str, Chain]:
    chains = {c.key: c for c in _DEFAULTS}

    raw = os.getenv("EXTRA_CHAINS", "").strip()
    if raw:
        try:
            for item in json.loads(raw):
                key = str(item["key"]).lower()
                chains[key] = Chain(
                    key=key, name=item.get("name", key.title()), emoji=item.get("emoji", "⛓"),
                    chain_id=item.get("chain_id"), pool=item.get("pool", ""), rpcs=_split(item["rpc"]),
                )
        except Exception:  # noqa: BLE001
            log.exception("EXTRA_CHAINS: ошибка разбора JSON, пропускаю")

    for c in chains.values():
        pool_env = os.getenv(f"POOL_{c.key.upper()}", "").strip()
        if pool_env:
            c.pool, c.verified = pool_env, False
        rpc_env = os.getenv(f"RPC_{c.key.upper()}", "").strip()
        if rpc_env:  # свой RPC (Alchemy, Infura...) идёт первым
            c.rpcs = _split(rpc_env) + c.rpcs
        if not c.pool:
            c.status, c.detail = "no_pool", f"укажите POOL_{c.key.upper()}"
        elif not ADDRESS_RE.match(c.pool):
            c.status, c.detail = "bad_config", "некорректный адрес пула"
    return chains


CHAINS: Dict[str, Chain] = _build()


def enabled_chains() -> List[Chain]:
    """Сети для опроса: учитывает фильтр CHAINS и результат самопроверки."""
    allow = {k.strip().lower() for k in os.getenv("CHAINS", "").split(",") if k.strip()}
    return [c for c in CHAINS.values()
            if (not allow or c.key in allow) and c.status not in ("bad_config", "no_pool")]


def chain_name(key: str) -> str:
    c = CHAINS.get(key)
    return c.name if c else key


def chain_emoji(key: str) -> str:
    c = CHAINS.get(key)
    return c.emoji if c else "⛓"
