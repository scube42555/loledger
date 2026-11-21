"""Hedera mirror-node txs -> wallet-perspective Movements.

HBAR: sum of the account's signed `transfers` (they include the fee legs when it paid); the
charged fee is split out as gas for the payer, staking-reward payouts are their own leg.
Tokens: net of `token_transfers` per token id. Counterparty: largest opposite-sign account that is
not the node, the fee account 0.0.98 or the staking reward accounts 0.0.800/0.0.801.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale

SYSTEM = {"0.0.98", "0.0.800", "0.0.801"}
REWARDS_CP = "hedera:staking_rewards"


def _note(t: dict[str, Any]) -> str:
    try:
        return base64.b64decode(t.get("memo_base64") or "").decode("utf-8", "replace")[:200]
    except (ValueError, TypeError):
        return ""


def normalize(chain: ChainConfig, wallet: str, history: list[dict[str, Any]], token_meta: Any
              ) -> tuple[list[Movement], dict[str, TxContext]]:
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for t in history:
        h = str(t["transaction_id"])
        ts = int(float(t["consensus_timestamp"]))
        ok = t.get("result") == "SUCCESS"
        payer = h.split("-")[0]
        fee = int(t.get("charged_tx_fee") or 0) if payer == wallet else 0
        skip = SYSTEM | {str(t.get("node") or "")}
        reward = sum(int(x["amount"]) for x in t.get("staking_reward_transfers") or [] if x["account"] == wallet)
        legs: list[Movement] = []

        def mk(leg: str, asset: str, raw: int, cp: str, source: str, sym: str, dec: int, kind: AssetKind) -> Movement:
            return Movement(chain=chain.name, tx_hash=h, leg_id=leg, block=0, timestamp=ts, wallet=wallet, asset_id=asset,
                            asset_kind=kind, symbol=sym, decimals=dec, amount=scale(raw, dec), counterparty=cp, source=source)

        hb = sum(int(x["amount"]) for x in t.get("transfers") or [] if x["account"] == wallet)
        hb_ex = hb + fee - reward
        if hb_ex:
            others = [x for x in t.get("transfers") or [] if x["account"] != wallet and x["account"] not in skip
                      and (int(x["amount"]) > 0) != (hb_ex > 0)]
            cp = max(others, key=lambda x: abs(int(x["amount"])))["account"] if others else ""
            legs.append(mk("hbar", NATIVE_ASSET, hb_ex, str(cp), "hedera", chain.native_symbol, 8, AssetKind.NATIVE))
        if reward:
            legs.append(mk("hbar:reward", NATIVE_ASSET, reward, REWARDS_CP, "reward_withdrawal", chain.native_symbol, 8,
                           AssetKind.NATIVE))
        tok: dict[str, int] = defaultdict(int)
        tok_cp: dict[str, str] = {}
        for x in t.get("token_transfers") or []:
            if x["account"] == wallet:
                tok[x["token_id"]] += int(x["amount"])
        for x in t.get("token_transfers") or []:
            if x["account"] != wallet and x["token_id"] in tok and (int(x["amount"]) > 0) != (tok[x["token_id"]] > 0):
                tok_cp.setdefault(x["token_id"], x["account"])
        for tid, raw in tok.items():
            if raw:
                sym, dec = token_meta(tid)
                legs.append(mk(f"token:{tid}", tid, raw, tok_cp.get(tid, ""), "hedera", sym, dec, AssetKind.ERC20))
        if not ok:
            legs = []
        moves.extend(legs)
        if legs or payer == wallet:
            txs[h] = TxContext(chain=chain.name, tx_hash=h, block=0, timestamp=ts, tx_from=payer,
                               tx_to=next((m.counterparty for m in legs), ""), method_id="", function_name=str(t.get("name")),
                               is_error=not ok, gas_fee=scale(int(t.get("charged_tx_fee") or 0), 8), value=Decimal(0),
                               meta={"note": _note(t), "tx_hash_b64": t.get("transaction_hash")})
    return moves, txs
