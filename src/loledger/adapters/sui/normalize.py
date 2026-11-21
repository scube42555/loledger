"""Sui tx -> wallet-perspective Movements from balanceChanges (exact), SUI gas split out."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale

SUI_TYPE = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"
FRAMEWORK = {"0x0000000000000000000000000000000000000000000000000000000000000001",
             "0x0000000000000000000000000000000000000000000000000000000000000002",
             # oracle price updates that precede swaps (Wormhole VAA verification, Pyth feeds)
             "0x5306f64e312b581766351c07af79c72fcb1cd25147157fdc2f8ad76de9a3fb6a",
             "0x04e20ddf36af412a4096f9014f4a565af9e812db9a05cc40254846cf6ed0ad91"}




def normalize(chain: ChainConfig, wallet: str, history: list[dict[str, Any]], meta_of: Any
              ) -> tuple[list[Movement], dict[str, TxContext]]:
    """meta_of(coin_type) -> (symbol, decimals)."""
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for t in history:
        eff = t["effects"]
        dig = t["digest"]
        ts = int(_ts(eff["timestamp"]))
        chk = int((eff.get("checkpoint") or {}).get("sequenceNumber") or 0)
        sender = (t.get("sender") or {}).get("address") or ""
        g = (eff.get("gasEffects") or {}).get("gasSummary") or {}
        gas = int(g.get("computationCost") or 0) + int(g.get("storageCost") or 0) - int(g.get("storageRebate") or 0)
        calls = [f'{c["function"]["module"]["package"]["address"]}::{c["function"]["module"]["name"]}::{c["function"]["name"]}'
                 for c in ((t.get("kind") or {}).get("commands") or {}).get("nodes") or [] if c.get("function")]
        events = [{"type": e["contents"]["type"]["repr"], "json": e["contents"]["json"]}
                  for e in (eff.get("events") or {}).get("nodes") or []]
        changes = [(((b.get("owner") or {}).get("address") or ""), b["coinType"]["repr"], int(b["amount"]))
                   for b in (eff.get("balanceChanges") or {}).get("nodes") or []]
        main = next((c.rsplit("::", 2)[0] for c in calls if c.rsplit("::", 2)[0] not in FRAMEWORK), "")
        per: dict[str, int] = defaultdict(int)
        for owner, ct, amt in changes:
            if owner == wallet:
                per[ct] += amt
        if sender == wallet and eff["status"] == "SUCCESS":
            per[SUI_TYPE] += gas  # balance change already includes gas; gas is booked as the fee
        legs: list[Movement] = []
        for ct, raw in per.items():
            if raw == 0:
                continue
            native = ct == SUI_TYPE
            sym, dec = ("SUI", 9) if native else meta_of(ct)
            cps = [o for o, c2, a in changes if c2 == ct and o != wallet and (a > 0) != (raw > 0)]
            legs.append(Movement(chain=chain.name, tx_hash=dig, leg_id=f"sui:{ct[-24:]}", block=chk, timestamp=ts,
                                 wallet=wallet, asset_id=NATIVE_ASSET if native else ct,
                                 asset_kind=AssetKind.NATIVE if native else AssetKind.ERC20, symbol=sym, decimals=dec,
                                 amount=scale(raw, dec), counterparty=cps[0] if cps else (main or sender), source="sui"))
        moves.extend(legs)
        if legs or sender == wallet:
            txs[dig] = TxContext(chain=chain.name, tx_hash=dig, block=chk, timestamp=ts, tx_from=sender, tx_to=main,
                                 method_id="", function_name=",".join(c.split("::", 1)[1] for c in calls[:3]),
                                 is_error=eff["status"] != "SUCCESS",
                                 # net gas can be negative: a storage rebate larger than the tx's costs
                                 gas_fee=scale(gas, 9), value=Decimal(0),
                                 meta={"calls": calls, "events": events})
    return moves, txs


def _ts(s: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
