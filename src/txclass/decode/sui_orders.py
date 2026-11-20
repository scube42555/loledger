"""Sui order lifecycle: DeepTrade limit orders (escrowed, linked by order_id) and DeepBook v3 orders
through a BalanceManager (linked by balance_manager_id). Everything else -> shared rules.

DeepTrade (limit_order::OrderPlacedEvent / OrderCanceledEvent): every order ends in an
OrderCanceledEvent carrying remaining_pay_amount / remaining_target_amount, whether the owner
cancelled, the keeper expired it, or the keeper filled it (cancel_order_by_keeper + flash loan).
filled = total_pay - remaining_pay  -> filled | partial | cancelled, exact.
DeepBook v3: wallet -> BalanceManager deposits in a tx that places orders are ORDER_PLACED; later
BalanceManager -> wallet withdrawals are ORDER_FILLED (another asset) or ORDER_CANCELLED (refund).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..model import NATIVE_ASSET, Movement, TxContext
from ..rules import Classified, Cls, RuleContext, _with_gas, classify_group

SUI_COIN = "0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"


def _coin_matches(leg: Movement, coin: str) -> bool:
    c = coin.lower().removeprefix("0x")
    if leg.asset_id == NATIVE_ASSET:
        return c.endswith("::sui::sui") or c == SUI_COIN.lower()
    return leg.asset_id.lower().removeprefix("0x") == c or leg.asset_id.lower().endswith(c)


@dataclass
class DtOrder:
    order_id: str
    pay_coin: str
    target_coin: str
    total_pay: int
    rate: str
    expire_ms: int
    placed_tx: str
    rows: list[Classified] = field(default_factory=list)
    outcome: str = ""
    settled: list[str] = field(default_factory=list)


def _ev(tx: TxContext, suffix: str, wallet: str, owner_key: str) -> list[dict[str, Any]]:
    return [e["json"] for e in tx.meta.get("events", []) if e["type"].split("<")[0].endswith(suffix)
            and str(e["json"].get(owner_key, "")) == wallet]


def classify_sui_wallet(wallet: str, moves: list[Movement], txs: dict[str, TxContext], ctx: RuleContext) -> list[Classified]:
    groups: dict[str, list[Movement]] = defaultdict(list)
    for m in moves:
        groups[m.tx_hash].append(m)
    for h, t in txs.items():
        if t.tx_from == wallet and h not in groups:
            groups[h] = []
    orders: dict[str, DtOrder] = {}
    bm_orders: dict[str, list[tuple[str, set[str], list[Classified]]]] = defaultdict(list)  # bm -> (tx, assets, rows)
    out: list[Classified] = []
    for h in sorted(groups, key=lambda h: (txs[h].timestamp, txs[h].block, h)):
        tx, legs = txs[h], groups[h]
        placed = _ev(tx, "::limit_order::OrderPlacedEvent", wallet, "owner")
        cancels = _ev(tx, "::limit_order::OrderCanceledEvent", wallet, "owner")
        db_placed = _ev(tx, "::order_info::OrderPlaced", wallet, "trader")
        bm_events = [e["json"] for e in tx.meta.get("events", []) if e["type"].endswith("::balance_manager::BalanceEvent")]
        calls = tx.meta.get("calls", [])
        rows: list[Classified] = []

        def row(m: Movement, cls: Cls, sub: str, reason: str, *, proto: str, role: str, oid: str = "",
                linked: str = "", details: str = "") -> Classified:
            r = Classified(m, tx, cls, sub, proto, 1.0, reason, role, linked)
            r.order_id, r.order_details = oid, details
            rows.append(r)
            return r

        if placed:
            ids = []
            for p in placed:
                o = DtOrder(str(p["order_id"]), str(p["pay_coin"]), str(p["target_coin"]), int(p["total_pay_amount"]),
                            str(p.get("rate", "")), int(p.get("expire_ts") or 0), h)
                orders[o.order_id] = o
                ids.append(o)
            for m in legs:
                o = next((x for x in ids if _coin_matches(m, x.pay_coin)), ids[0])
                exp = datetime.fromtimestamp(o.expire_ms / 1000, UTC).strftime("%Y-%m-%d") if o.expire_ms else "-"
                r = row(m, Cls.ORDER_PLACED, "deeptrade_limit", f"DeepTrade limit order {o.order_id[:12]}… placed (escrowed)",
                        proto="deeptrade", role="order" if m.amount < 0 else "change", oid=f"deeptrade:{o.order_id}",
                        details=f"pay {o.total_pay} raw {o.pay_coin.split('::')[-1]} for {o.target_coin.split('::')[-1]} "
                                f"at rate {o.rate}; expires {exp}")
                o.rows.append(r)
        elif cancels:
            for c in cancels:
                found = orders.get(str(c["order_id"]))
                if found is None:
                    continue
                o = found
                remaining = int(c.get("remaining_pay_amount") or 0)
                filled = o.total_pay - remaining
                outcome = "cancelled" if filled <= 0 else ("filled" if remaining == 0 else "partial")
                by = "keeper" if tx.tx_from != wallet else "owner"
                o.outcome = outcome if by == "owner" or outcome != "cancelled" else "expired_or_cancelled_by_keeper"
                o.settled.append(h)
                pct = f"{filled * 100 / o.total_pay:.1f}%" if o.total_pay else "?"
                for m in legs:
                    if _coin_matches(m, o.target_coin) and m.amount > 0:
                        row(m, Cls.ORDER_FILLED, "deeptrade_limit", f"DeepTrade order {o.order_id[:12]}… filled {pct} "
                            f"(settled by {by})", proto="deeptrade", role="in", oid=f"deeptrade:{o.order_id}", linked=o.placed_tx)
                    elif _coin_matches(m, o.pay_coin) and m.amount > 0:
                        row(m, Cls.ORDER_CANCELLED, "deeptrade_limit", f"DeepTrade order {o.order_id[:12]}… remaining pay "
                            f"returned (cancel reason {c.get('cancel_reason')}, by {by})", proto="deeptrade", role="refund",
                            oid=f"deeptrade:{o.order_id}", linked=o.placed_tx)
                for r in o.rows:
                    r.linked_tx_hash = r.linked_tx_hash or h
            used = {id(r.movement) for r in rows}
            rest = [m for m in legs if id(m) not in used]
            if rest:
                rows.extend(classify_group(wallet, tx, rest, ctx))
        elif db_placed or (bm_events and any(str(e.get("deposit")) == "True" for e in bm_events) and
                           any("place_limit_order" in c for c in calls)):
            bms = {str(e.get("balance_manager_id")) for e in bm_events} | {str(p.get("balance_manager_id")) for p in db_placed}
            oids = ",".join(str(p.get("order_id")) for p in db_placed)
            assets = {m.asset_id for m in legs if m.amount < 0}
            for m in legs:
                row(m, Cls.ORDER_PLACED, "deepbook_limit", "DeepBook v3 order(s) placed via balance manager "
                    f"{next(iter(bms))[:12]}…", proto="deepbook-v3", role="order" if m.amount < 0 else "change",
                    oid=f"deepbook:{oids[:60]}", details="; ".join(
                        f"{'bid' if str(p.get('is_bid')) == 'True' else 'ask'} {p.get('placed_quantity')} @ {p.get('price')} (raw)"
                        for p in db_placed[:3]))
            for bm in bms:
                bm_orders[bm].append((h, assets, list(rows)))
        elif bm_events and any(str(e.get("deposit")) == "False" for e in bm_events) and legs:
            wbms = [str(e.get("balance_manager_id")) for e in bm_events if str(e.get("deposit")) == "False"]
            prior = [x for bm in wbms for x in bm_orders.get(bm, [])]
            if prior:
                deposited = set().union(*(a for _, a, _ in prior))
                placed_tx = prior[-1][0]
                for m in legs:
                    if m.amount > 0:
                        cls = Cls.ORDER_CANCELLED if m.asset_id in deposited else Cls.ORDER_FILLED
                        row(m, cls, "deepbook_limit", f"withdrawal from DeepBook balance manager {wbms[0][:12]}… "
                            f"({'unfilled funds back' if cls == Cls.ORDER_CANCELLED else 'filled proceeds'})",
                            proto="deepbook-v3", role="in", linked=placed_tx)
                    else:
                        rows.extend(classify_group(wallet, tx, [m], ctx))
                for _, _, prs in prior:
                    for r in prs:
                        r.linked_tx_hash = r.linked_tx_hash or h
            else:
                rows.extend(classify_group(wallet, tx, legs, ctx))
        else:
            rows.extend(classify_group(wallet, tx, legs, ctx))
        out.extend(_with_gas(rows, wallet, tx) if rows and rows[0].movement.source != "gas" else rows)
        if not rows and tx.tx_from == wallet:
            out.extend(classify_group(wallet, tx, [], ctx))
    for o in orders.values():
        outcome = o.outcome or "open"
        for r in o.rows:
            r.subtype = f"deeptrade_limit:{outcome}"
    return out
