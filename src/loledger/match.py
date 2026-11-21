"""Heuristic matching of flows the deterministic stages could not link.

Events are (wallet, tx) groups. Outflows: TRANSFER out, OTHER/bridge_out, unresolved ORDER_PLACED.
Inflows: TRANSFER in, OTHER/bridge_in, OTHER/claim. Spam, dust (< min_usd) and already linked
groups are ignored. Every (out, later in) pair inside the window gets a score in [0, 1]:

  value   1 - |usd_in/usd_out - expected_ratio| / value_tolerance      (fees make ratio < 1)
  time    exp(-gap / time_tau)
  kind    exchanger swap (different asset), bridge (same asset family, other chain) or
          order fill (different asset after an unresolved order; satisfies the decoded limit)
  score = weighted mean; pairs below min_score are dropped.

Assignment is greedy by score, one-to-one. Confidence = conf_base + conf_span * score (< 1.0).
Unmatched events are left as they are (never forced).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .registry import norm_symbol
from .rules import Classified, Cls

DEFAULTS: dict[str, Any] = {
    "min_usd": 5.0,
    "window_hours": 72.0,
    "order_window_days": 30.0,
    "expected_ratio": 0.98,
    "value_tolerance": 0.08,
    "time_tau_hours": 2.0,
    "order_time_tau_hours": 72.0,
    "weights": {"value": 0.55, "time": 0.25, "kind": 0.20},
    "min_score": 0.6,
    "conf_base": 0.5,
    "conf_span": 0.45,
}

OUT_KINDS = {("TRANSFER", "to_eoa"), ("TRANSFER", "to_contract"), ("OTHER", "bridge_out")}
IN_KINDS = {("TRANSFER", "from_eoa"), ("TRANSFER", "from_contract"), ("TRANSFER", "from_exchange"),
            ("OTHER", "bridge_in"), ("OTHER", "claim")}
UNRESOLVED = ("open", "spent_outside", "escrow_open")
LIMIT = re.compile(r"for ≥([0-9.]+) (\S+)")


@dataclass
class FlowEvent:
    chain: str
    wallet: str
    tx: str
    ts: int
    out: bool
    usd: Decimal
    assets: dict[str, Decimal]  # symbol family -> amount (abs)
    kind: str  # transfer | bridge | order
    rows: list[Classified] = field(default_factory=list)
    min_receive: tuple[Decimal, str] | None = None  # (amount, symbol) from decoded order terms
    counterparties: set[str] = field(default_factory=set)


@dataclass
class Match:
    out: FlowEvent
    inn: FlowEvent
    kind: str
    score: float
    detail: str


def _events(rows: list[Classified], cfg: dict[str, Any]) -> tuple[list[FlowEvent], list[FlowEvent]]:
    groups: dict[tuple[str, str], list[Classified]] = defaultdict(list)
    for r in rows:
        groups[(r.movement.wallet, r.tx.tx_hash)].append(r)
    outs: list[FlowEvent] = []
    ins: list[FlowEvent] = []
    for (wallet, h), rs in groups.items():
        legs = [r for r in rs if r.movement.source not in ("gas", "min_ada") and r.leg_role != "fee"]
        if not legs or any(r.linked_tx_hash for r in legs if r.classification != Cls.ORDER_PLACED):
            continue
        c, sub = legs[0].classification.value, legs[0].subtype
        if legs[0].classification == Cls.ORDER_PLACED:
            if not any(u in sub for u in UNRESOLVED):
                continue
            by_order: dict[str, list[Classified]] = defaultdict(list)
            for r in legs:
                if r.movement.amount < 0 and any(u in r.subtype for u in UNRESOLVED):
                    by_order[r.order_id].append(r)
            for olegs in by_order.values():  # one event per order (a tx can place several)
                ev = _event(olegs, wallet, h, "order", True, cfg)
                if ev:
                    mt = LIMIT.search(olegs[0].order_details)
                    if mt:
                        ev.min_receive = (Decimal(mt.group(1)), norm_symbol(mt.group(2)).removeprefix("w"))
                    outs.append(ev)
            continue
        elif (c, sub) in OUT_KINDS:
            kind, is_out = ("bridge" if sub == "bridge_out" else "transfer"), True
        elif (c, sub) in IN_KINDS:
            kind, is_out = ("bridge" if sub == "bridge_in" else "transfer"), False
        else:
            continue
        ev = _event(legs, wallet, h, kind, is_out, cfg, all_rows=rs)
        if ev:
            (outs if is_out else ins).append(ev)
    return outs, ins


def _event(legs: list[Classified], wallet: str, h: str, kind: str, is_out: bool, cfg: dict[str, Any],
           all_rows: list[Classified] | None = None) -> FlowEvent | None:
    priced = all(r.usd_value is not None for r in legs)
    usd = abs(sum((r.usd_value or Decimal(0) for r in legs), Decimal(0))) if priced else Decimal(-1)
    if priced and usd < Decimal(str(cfg["min_usd"])):
        return None
    assets: dict[str, Decimal] = defaultdict(Decimal)
    for r in legs:
        assets[norm_symbol(r.movement.symbol).removeprefix("w")] += abs(r.movement.amount)
    return FlowEvent(legs[0].movement.chain, wallet, h, legs[0].tx.timestamp, is_out, usd, dict(assets), kind,
                     all_rows if all_rows is not None else legs, counterparties={r.movement.counterparty for r in legs})


def _score(o: FlowEvent, i: FlowEvent, cfg: dict[str, Any]) -> Match | None:
    gap = i.ts - o.ts
    if gap < 0:
        return None
    if o.wallet == i.wallet and o.tx == i.tx:
        return None
    same_family = bool(set(o.assets) & set(i.assets))
    w = cfg["weights"]
    if o.kind == "order":
        if gap > cfg["order_window_days"] * 86_400 or i.wallet != o.wallet or same_family:
            return None
        kind = "order_fill"
        tau = cfg["order_time_tau_hours"] * 3600
        kind_score = 1.0 if o.counterparties & i.counterparties else 0.4  # paid out by the order's own script
        if o.min_receive:
            amt, sym = o.min_receive
            if sym not in i.assets:
                return None
            kind_score = 1.0 if i.assets[sym] >= amt else 0.0
    else:
        if gap > cfg["window_hours"] * 3600:
            return None
        tau = cfg["time_tau_hours"] * 3600
        if same_family:
            if o.chain == i.chain:
                return None  # same asset back on the same chain is a plain transfer pair, not evidence
            kind, kind_score = "bridge", 1.0 if (o.kind == "bridge" or i.kind == "bridge") else 0.7
        else:
            kind, kind_score = "exchanger_swap", 0.8
    if same_family and o.kind != "order":
        fam = next(iter(set(o.assets) & set(i.assets)))
        ratio = float(i.assets[fam] / o.assets[fam]) if o.assets[fam] else 0.0  # amounts: no price needed
    elif o.usd <= 0 or i.usd <= 0:
        return None  # different assets need USD values on both sides
    else:
        ratio = float(i.usd / o.usd) if o.usd else 0.0
    if o.kind == "order" and not (o.counterparties & i.counterparties) and not o.min_receive:
        # no terms and no shared script: accept only an immediate payout of matching value
        if gap > 3600 or not 0.85 <= ratio <= 1.05:
            return None
    value = max(0.0, 1 - abs(ratio - cfg["expected_ratio"]) / cfg["value_tolerance"])
    if o.kind == "order":
        value = 1.0 if ratio >= 0.85 else max(0.0, ratio / 0.85)
    time = math.exp(-gap / tau)
    score = (w["value"] * value + w["time"] * time + w["kind"] * kind_score) / (w["value"] + w["time"] + w["kind"])
    if score < cfg["min_score"] or value == 0.0:
        return None
    same_cp = " same counterparty script," if o.counterparties & i.counterparties else ""
    value_txt = (f"value ${o.usd:.2f} → ${i.usd:.2f}" if o.usd > 0 and i.usd > 0
                 else "amount " + ", ".join(f"{v.normalize():f}" for v in o.assets.values()) + " → " +
                 ", ".join(f"{v.normalize():f}" for v in i.assets.values()))
    detail = (f"{kind}:{same_cp} {o.chain} {o.tx[:12]}… → {i.chain} {i.tx[:12]}…, Δt={gap / 3600:.1f}h, "
              f"{value_txt} (ratio {ratio:.3f}), score {score:.2f}")
    return Match(o, i, kind, score, detail)


def match_flows(rows: list[Classified], settings: dict[str, Any] | None = None) -> list[Match]:
    cfg = {**DEFAULTS, **(settings or {})}
    cfg["weights"] = {**DEFAULTS["weights"], **(settings or {}).get("weights", {})}
    outs, ins = _events(rows, cfg)
    cands = [m for o in outs for i in ins if (m := _score(o, i, cfg))]
    cands.sort(key=lambda m: -m.score)
    used: set[int] = set()
    chosen: list[Match] = []
    for m in cands:
        if id(m.out) in used or id(m.inn) in used:
            continue
        used |= {id(m.out), id(m.inn)}
        chosen.append(m)
    for m in chosen:
        conf = round(cfg["conf_base"] + cfg["conf_span"] * m.score, 2)
        for ev, other in ((m.out, m.inn), (m.inn, m.out)):
            for r in ev.rows:
                r.linked_tx_hash = r.linked_tx_hash or other.tx
                r.confidence = min(r.confidence, conf)
                r.reason += f"; heuristic match ({m.detail})"
                if m.kind == "order_fill":
                    if ev is m.inn and r.movement.amount > 0:
                        r.classification, r.subtype = Cls.ORDER_FILLED, "heuristic"
                    elif ev is m.out and r.classification == Cls.ORDER_PLACED:
                        r.subtype = r.subtype.split(":")[0] + ":heuristic_filled"
                elif r.classification == Cls.TRANSFER:
                    side = "out" if ev is m.out else "in"
                    r.subtype = f"{m.kind}_{side}"
    return chosen
