"""Attach USD prices to classified rows (value at time + gas), with a Cardano fallback.

Fallback: a Cardano token DefiLlama does not price gets the ADA price implied by the wallet's own
order fills (nearest fill in time) times the ADA/USD price. Those rows say price_source=implied:own_fill.
Spam rows and NFT/1155 legs are left unpriced.
"""

from __future__ import annotations

import bisect
import logging
import re
from collections import defaultdict
from decimal import Decimal

from .config import ChainConfig
from .model import NATIVE_ASSET, AssetKind
from .prices import PriceService, coin_id, hour
from .rules import Classified

log = logging.getLogger(__name__)
EXEC = re.compile(r"executed ≈?([0-9.eE+-]+) ADA/(\S+)")
IMPLIED_MAX_AGE = 3 * 86_400  # an own-fill price older/newer than this is not used


def _priceable(r: Classified) -> bool:
    return not r.subtype.startswith("spam_") and r.movement.asset_kind not in (AssetKind.ERC721, AssetKind.ERC1155)


def apply_prices(rows: list[Classified], chains: dict[str, ChainConfig], svc: PriceService,
                 aliases: dict[str, dict[str, str]] | None = None) -> dict[str, int]:
    """aliases: chain -> {asset_id: fallback coin id} (bridged tokens -> L1 asset)."""
    aliases = aliases or {}
    wanted: set[tuple[str, int]] = set()
    for r in rows:
        ch = chains[r.movement.chain]
        if _priceable(r):
            c = coin_id(ch, r.movement)
            if c:
                wanted.add((c, hour(r.movement.timestamp)))
            alias = aliases.get(ch.name, {}).get(r.movement.asset_id)
            if alias:
                wanted.add((alias, hour(r.movement.timestamp)))
            if r.movement.asset_kind == AssetKind.CARDANO and ch.native_price_id:  # for the implied fallback
                wanted.add((ch.native_price_id, hour(r.movement.timestamp)))
        if r.gas_fee_native and ch.native_price_id:
            wanted.add((ch.native_price_id, hour(r.tx.timestamp)))
    svc.prefetch(wanted)

    implied = _implied_cardano(rows)
    stats: dict[str, int] = defaultdict(int)
    for r in rows:
        m, ch = r.movement, chains[r.movement.chain]
        if r.gas_fee_native and ch.native_price_id:
            p = svc.get(ch.native_price_id, r.tx.timestamp)
            r.gas_fee_usd = r.gas_fee_native * p if p is not None else None
        if not _priceable(r):
            stats["skipped_spam_or_nft"] += 1
            continue
        c = coin_id(ch, m)
        p = svc.get(c, m.timestamp) if c else None
        src = "defillama" if p is not None else ""
        alias = aliases.get(ch.name, {}).get(m.asset_id)
        if p is None and alias:
            p = svc.get(alias, m.timestamp)
            src = f"defillama:{alias}" if p is not None else ""
        if p is None and m.asset_kind == AssetKind.CARDANO and m.asset_id in implied:
            ada = svc.get(ch.native_price_id, m.timestamp)
            near = _nearest(implied[m.asset_id], m.timestamp)
            if ada is not None and near is not None and abs(near[0] - m.timestamp) <= IMPLIED_MAX_AGE:
                p = near[1] * ada
                src = f"implied:own_fill({abs(near[0] - m.timestamp) / 86_400:.1f}d)"
        if p is None:
            stats["unpriced"] += 1
            continue
        r.usd_price, r.usd_value, r.price_source = p, m.amount * p, src
        stats[src.split("(")[0]] += 1
    return dict(stats)


def _implied_cardano(rows: list[Classified]) -> dict[str, list[tuple[int, Decimal]]]:
    out: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
    for r in rows:
        if r.movement.chain != "Cardano" or r.movement.asset_id == NATIVE_ASSET:
            continue
        mt = EXEC.search(r.order_details)
        if mt and mt.group(2) == r.movement.symbol:
            out[r.movement.asset_id].append((r.movement.timestamp, Decimal(mt.group(1))))
    for v in out.values():
        v.sort()
    return out


def _nearest(series: list[tuple[int, Decimal]], ts: int) -> tuple[int, Decimal] | None:
    if not series:
        return None
    i = bisect.bisect_left(series, (ts, Decimal(0)))
    cands = series[max(0, i - 1): i + 1]
    return min(cands, key=lambda x: abs(x[0] - ts))
