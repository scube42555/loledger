"""Historical USD prices from DefiLlama (free, keyless), cached per (coin, hour) in the SQLite store.

Coin ids: "<price_chain>:<token address>" for ERC-20, "cardano:<policy><name>" for Cardano native
tokens, and the chain's native_price_id (e.g. "coingecko:cardano") for the native asset.
Lookups use the hour bucket of the movement timestamp; DefiLlama returns the nearest price within
the search window. Misses are cached too (as null) once the hour is older than a day.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal

from .config import ChainConfig
from .http import HttpClient
from .model import NATIVE_ASSET, AssetKind, Movement
from .store import Store

log = logging.getLogger(__name__)

URL = "https://coins.llama.fi/batchHistorical"
HOUR = 3600
BATCH_POINTS = 120
SEARCH_WIDTH = "4h"
MISSING = "missing"


def coin_id(chain: ChainConfig, m: Movement) -> str | None:
    if m.asset_kind == AssetKind.EXCHANGE:
        from .decode.exchanges import exchange_price_ids

        return exchange_price_ids().get(m.asset_id.upper())
    if m.asset_id == NATIVE_ASSET:
        return chain.native_price_id or None
    if m.asset_id.startswith("peggy0x"):  # Injective Peggy-bridged ERC-20 prices as the Ethereum token
        return f"ethereum:{m.asset_id[5:]}"
    if m.asset_kind == AssetKind.ERC20 and chain.price_chain:
        return f"{chain.price_chain}:{m.asset_id}"
    if m.asset_kind == AssetKind.CARDANO:
        return "cardano:" + m.asset_id.replace(".", "")
    return None  # NFTs / 1155 positions are not priced


def hour(ts: int) -> int:
    return ts - ts % HOUR + HOUR // 2  # middle of the hour


class PriceService:
    def __init__(self, store: Store, http: HttpClient | None = None) -> None:
        self.store = store
        self.http = http or HttpClient(2)
        self.mem: dict[tuple[str, int], Decimal | None] = {}

    def _cached(self, coin: str, h: int) -> Decimal | None | str:
        v = self.store.get_raw("defillama", "price", {"coin": coin, "hour": h})
        if v is None:
            return MISSING
        return None if v == "none" else Decimal(str(v))

    def prefetch(self, wanted: Iterable[tuple[str, int]]) -> None:
        todo: dict[str, list[int]] = defaultdict(list)
        for coin, h in set(wanted):
            if coin.startswith("fixed:"):
                continue
            if (coin, h) in self.mem:
                continue
            c = self._cached(coin, h)
            if c is MISSING:
                todo[coin].append(h)
            else:
                self.mem[(coin, h)] = c  # type: ignore[assignment]
        points = [(c, h) for c, hs in todo.items() for h in sorted(hs)]
        for i in range(0, len(points), BATCH_POINTS):
            chunk: dict[str, list[int]] = defaultdict(list)
            for c, h in points[i:i + BATCH_POINTS]:
                chunk[c].append(h)
            body = self.http.request_json("GET", URL, params={"coins": json.dumps(chunk), "searchWidth": SEARCH_WIDTH})
            got = (body or {}).get("coins", {})
            for c, hs in chunk.items():
                prices = [(int(p["timestamp"]), Decimal(str(p["price"]))) for p in (got.get(c) or {}).get("prices", [])]
                for h in hs:
                    best = min(prices, key=lambda p: abs(p[0] - h), default=None)
                    val = best[1] if best and abs(best[0] - h) <= 4 * HOUR else None
                    self.mem[(c, h)] = val
                    if val is not None or h < time.time() - 86_400:
                        self.store.put_raw("defillama", "price", {"coin": c, "hour": h}, str(val) if val is not None else "none")
            log.info("  prices %d/%d", min(i + BATCH_POINTS, len(points)), len(points))

    def get(self, coin: str, ts: int) -> Decimal | None:
        if coin == "fixed:usd":
            return Decimal(1)
        return self.mem.get((coin, hour(ts)))
