"""Injective (Cosmos SDK) tx logs -> wallet-perspective Movements.

transfer events (sender, recipient, amount) give exact legs with counterparties; the fee is booked
as gas for the fee payer. Two credits happen without a tx and are synthesized from tx data:
  * unbonding completion: `unbond` event (amount, completion_time) -> + amount at completion_time
  * Peggy (Ethereum bridge) deposits: MsgDepositClaim votes (deduplicated by event_nonce) -> one mint
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale

INJ_ERC20 = "0xe28b3b32b6c345a34ff64674606124dd5aceca30"
_COIN = re.compile(r"^(\d+)(.+)$")


def coins(s: str) -> list[tuple[int, str]]:
    out = []
    for part in (s or "").split(","):
        mt = _COIN.match(part.strip())
        if mt:
            out.append((int(mt.group(1)), mt.group(2)))
    return out


def _ts(s: str) -> int:
    s = s.replace(" +0000 UTC", "+00:00").replace("Z", "+00:00")
    if "." in s and "+" in s:
        head, tz = s.split("+")
        s = head[:26] + "+" + tz
    return int(datetime.fromisoformat(s).timestamp())


def normalize(chain: ChainConfig, wallet: str, history: list[dict[str, Any]], modules: dict[str, str],
              denom_meta: Any) -> tuple[list[Movement], dict[str, TxContext]]:
    """denom_meta(denom) -> (symbol, decimals)."""
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    deposits: dict[str, tuple[int, str, int, str]] = {}  # nonce -> (amount, token, earliest ts, eth sender)

    def mk(h: str, leg: str, ts: int, blk: int, denom: str, raw: int, cp: str, source: str) -> Movement:
        native = denom == "inj"
        sym, dec = (chain.native_symbol, 18) if native else denom_meta(denom)
        return Movement(chain=chain.name, tx_hash=h, leg_id=leg, block=blk, timestamp=ts, wallet=wallet,
                        asset_id=NATIVE_ASSET if native else denom, asset_kind=AssetKind.NATIVE if native else AssetKind.ERC20,
                        symbol=sym, decimals=dec, amount=scale(raw, dec), counterparty=cp, source=source)

    for t in history:
        h, blk, ts = str(t["hash"]), int(t["block_number"]), _ts(str(t["block_timestamp"]))
        ok = int(t.get("code") or 0) == 0
        fee = t.get("gas_fee") or {}
        payer = str(fee.get("payer") or "")
        fee_inj = sum(int(c["amount"]) for c in fee.get("amount") or [] if c.get("denom") == "inj")
        msg_types = [str(m.get("type")) for m in t.get("messages") or []]
        legs: list[Movement] = []
        net: dict[tuple[str, str, str], int] = defaultdict(int)  # (denom, counterparty, source) -> signed raw
        if ok:
            for lg in t.get("logs") or []:
                spent: list[tuple[str, int, str]] = []  # (spender, raw, denom)
                received: list[tuple[str, int, str]] = []
                for e in lg.get("events") or []:
                    attrs = {a["key"]: a["value"] for a in e.get("attributes") or []}
                    if e["type"] in ("coin_spent", "coin_received"):
                        who = str(attrs.get("spender") if e["type"] == "coin_spent" else attrs.get("receiver"))
                        for raw, denom in coins(str(attrs.get("amount", ""))):
                            (spent if e["type"] == "coin_spent" else received).append((who, raw, denom))
                    elif e["type"] == "unbond" and any("MsgUndelegate" in m for m in msg_types):
                        for raw, denom in coins(str(attrs.get("amount", ""))):
                            when = _ts(str(attrs.get("completion_time", t["block_timestamp"])))
                            legs.append(mk(f"unbond:{h}", "unbonding_complete", when, blk, denom, raw,
                                           "injective:not_bonded_pool", "unbonding_complete"))
                            txs[f"unbond:{h}"] = TxContext(chain=chain.name, tx_hash=f"unbond:{h}", block=blk, timestamp=when,
                                                           tx_from="", tx_to="injective:unbonding", method_id="",
                                                           function_name="unbonding_complete", is_error=False,
                                                           gas_fee=Decimal(0), value=Decimal(0))
                # coin_spent/coin_received are complete (delegations emit no `transfer`); the counterparty is the
                # opposite event of the same amount and denom in the same message
                for who, raw, denom in spent:
                    if who == wallet:
                        cp = next((r for r, a, d in received if a == raw and d == denom and r != wallet), "")
                        net[(denom, cp, "transfer")] -= raw
                for who, raw, denom in received:
                    if who == wallet:
                        cp = next((sp for sp, a, d in spent if a == raw and d == denom and sp != wallet), "")
                        src = "reward_withdrawal" if modules.get(cp) == "distribution" else "transfer"
                        net[(denom, cp, src)] += raw
            for m in t.get("messages") or []:
                v = m.get("value") or {}
                if m.get("type") == "/injective.peggy.v1.MsgDepositClaim" and v.get("cosmos_receiver") == wallet:
                    n = str(v.get("event_nonce"))
                    prev = deposits.get(n)
                    if prev is None or ts < prev[2]:
                        deposits[n] = (int(v.get("amount") or 0), str(v.get("token_contract") or "").lower(), ts,
                                       str(v.get("ethereum_sender") or "").lower())
        n_leg = 0
        for (denom, cp, src), raw in net.items():
            if raw:
                n_leg += 1
                legs.append(mk(h, f"inj:{n_leg}", ts, blk, denom, raw, cp, src))
        main_legs = [m for m in legs if m.tx_hash == h]
        moves.extend(legs)
        if main_legs or payer == wallet:
            txs[h] = TxContext(chain=chain.name, tx_hash=h, block=blk, timestamp=ts, tx_from=payer,
                               tx_to=msg_types[0] if msg_types else "", method_id="",
                               function_name=",".join(x.rsplit(".", 1)[-1] for x in msg_types[:3]), is_error=not ok,
                               gas_fee=scale(fee_inj, 18) if payer else Decimal(0), value=Decimal(0),
                               meta={"msg_types": msg_types, "memo": t.get("memo") or ""})
    for n, (raw, token, ts, eth_sender) in deposits.items():
        denom = "inj" if token == INJ_ERC20 else f"peggy{token}"
        h = f"peggy:{n}"
        moves.append(mk(h, "peggy_mint", ts, 0, denom, raw, f"ethereum:{eth_sender}", "bridge_mint"))
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=0, timestamp=ts, tx_from=f"ethereum:{eth_sender}",
                           tx_to="injective:peggy", method_id="", function_name="peggy_deposit", is_error=False,
                           gas_fee=Decimal(0), value=Decimal(0), meta={"event_nonce": n, "token_contract": token})
    moves.sort(key=lambda m: (m.timestamp, m.tx_hash, m.leg_id))
    return moves, txs
