"""Algorand txs (incl. inner txns) -> wallet-perspective Movements.

Per (sub)txn affecting the wallet:
  pay:   sender: -amount (and -close-amount when closing); receiver/close-to: +amount / +close-amount
  axfer: same for the ASA (asset-sender = clawback source)
  root fee: paid by the root sender (inner fees are pooled from the app/outer txn)
  sender-/receiver-/close-rewards: legacy participation rewards credited at tx time
Reconciles: final balance == sum(legs) - fees paid.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale

REWARDS_CP = "algorand:rewards"


def _note(t: dict[str, Any]) -> str:
    import base64

    try:
        return base64.b64decode(t.get("note") or "").decode("utf-8", "replace")[:200]
    except (ValueError, TypeError):
        return ""


def _walk(t: dict[str, Any]) -> list[dict[str, Any]]:
    out = [t]
    for i in t.get("inner-txns") or []:
        out.extend(_walk(i))
    return out


def normalize(chain: ChainConfig, wallet: str, history: list[dict[str, Any]], asset_meta: Any
              ) -> tuple[list[Movement], dict[str, TxContext]]:
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for t in history:
        h, ts, rnd = t["id"], int(t["round-time"]), int(t["confirmed-round"])
        net: dict[str, int] = defaultdict(int)
        cps: dict[str, str] = {}
        rewards = 0
        for s in _walk(t):
            snd = s.get("sender")
            if s.get("tx-type") == "pay":
                p = s.get("payment-transaction") or {}
                key = NATIVE_ASSET
                amt, close, rcv, close_to = int(p.get("amount", 0)), int(p.get("close-amount", 0)), p.get("receiver"), p.get("close-remainder-to")
                src = snd
            elif s.get("tx-type") == "axfer":
                p = s.get("asset-transfer-transaction") or {}
                key = str(p.get("asset-id"))
                amt, close, rcv, close_to = int(p.get("amount", 0)), int(p.get("close-amount", 0)), p.get("receiver"), p.get("close-to")
                src = p.get("sender") or snd  # clawback moves from asset-sender
            else:
                src = rcv = close_to = None
                key, amt, close = NATIVE_ASSET, 0, 0
            if src == wallet:
                net[key] -= amt + close
                if amt or close:
                    cps.setdefault(key, str(rcv or close_to))
            if rcv == wallet:
                net[key] += amt
                cps.setdefault(key, str(src))
            if close_to == wallet:
                net[key] += close
                cps.setdefault(key, str(src))
            if snd == wallet:
                rewards += int(s.get("sender-rewards", 0))
            if rcv == wallet:
                rewards += int(s.get("receiver-rewards", 0))
            if close_to == wallet:
                rewards += int(s.get("close-rewards", 0))
        legs: list[Movement] = []
        for key, raw in net.items():
            if raw == 0:
                continue
            if key == NATIVE_ASSET:
                sym, dec, kind = chain.native_symbol, 6, AssetKind.NATIVE
            else:
                sym, dec = asset_meta(int(key))
                kind = AssetKind.ERC20
            legs.append(Movement(chain=chain.name, tx_hash=h, leg_id=f"algo:{key}", block=rnd, timestamp=ts, wallet=wallet,
                                 asset_id=key, asset_kind=kind, symbol=sym, decimals=dec, amount=scale(raw, dec),
                                 counterparty=cps.get(key, ""), source="algorand"))
        if rewards:
            legs.append(Movement(chain=chain.name, tx_hash=h, leg_id="algo:rewards", block=rnd, timestamp=ts, wallet=wallet,
                                 asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol, decimals=6,
                                 amount=scale(rewards, 6), counterparty=REWARDS_CP, source="reward_withdrawal"))
        moves.extend(legs)
        if legs or t.get("sender") == wallet:
            app = (t.get("application-transaction") or {}).get("application-id")
            note = _note(t)
            fn = str(t.get("tx-type"))
            if fn == "keyreg":
                fn = "consensus_keyreg"
            elif not legs and note.startswith("af/gov"):
                fn = "governance_vote"
            txs[h] = TxContext(chain=chain.name, tx_hash=h, block=rnd, timestamp=ts, tx_from=str(t.get("sender")),
                               tx_to=f"app:{app}" if app else str((t.get("payment-transaction") or {}).get("receiver") or ""),
                               method_id="", function_name=fn, is_error=False,
                               gas_fee=scale(int(t.get("fee", 0)), 6), value=Decimal(0),
                               meta={"tx_type": t.get("tx-type"), "note": note})
    return moves, txs
