"""Cardano classification: batcher order lifecycle (exact, by UTxO spend), staking, then generic rules.

tx1 (wallet-signed) sends funds to an order/request script with a datum   -> ORDER_PLACED
tx2 spends that exact UTxO (tx_hash#index):
    wallet-signed                                                         -> ORDER_CANCELLED
    batcher, pays out a different asset                                   -> ORDER_FILLED
    batcher, pays back the sold asset only                                -> ORDER_CANCELLED (refund)
    LP add / remove orders                                                -> LP_DEPOSIT / LP_WITHDRAW
    batcher re-creates an output at the same script                       -> partial fill; the new UTxO
                                                                             continues the same order
The link is exact, so these rows keep confidence 1.0 when the venue is registered; orders at
unregistered scripts are linked the same way but get 0.9 (venue unknown).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from typing import Any

import yaml

from ..adapters.cardano.normalize import DEPOSIT_CP, MINT_CP, REWARDS_CP, reward_account
from ..model import NATIVE_ASSET, Movement, TxContext
from ..registry import Registry
from ..rules import Classified, Cls, RuleContext, _with_gas, classify_group
from .cardano_datums import OrderTerms, decode

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScriptInfo:
    venue: str
    kind: str


def load_scripts() -> dict[str, ScriptInfo]:
    raw = yaml.safe_load(resources.files("txclass.registry").joinpath("cardano.yaml").read_text()) or {}
    return {h: ScriptInfo(str(v["venue"]), str(v["kind"])) for h, v in (raw.get("scripts") or {}).items()}


def load_cardano_price_aliases() -> dict[str, str]:
    raw = yaml.safe_load(resources.files("txclass.registry").joinpath("cardano.yaml").read_text()) or {}
    return {str(a): str(c) for a, c in (raw.get("price_aliases") or {}).items()}


@dataclass
class Order:
    ref: str
    wallet: str
    placed_tx: str
    script_hash: str
    venue: str
    kind: str  # market | limit | lp_add | lp_remove | staking | unknown
    sold: set[str]
    datum_summary: str
    registered: bool
    rows: list[Classified] = field(default_factory=list)
    settled_tx: str = ""
    outcome: str = ""
    terms: OrderTerms | None = None
    sold_amt: dict[str, Decimal] = field(default_factory=dict)
    fills: list[tuple[str, dict[str, Decimal]]] = field(default_factory=list)  # (tx, received per asset)
    settle_rows: list[Classified] = field(default_factory=list)
    order_id: str = ""


def order_kind(msg: str, script_kind: str | None) -> str:
    m = msg.lower()
    if "add liquidity" in m or "deposit liquidity" in m:
        return "lp_add"
    if "remove liquidity" in m or "withdraw liquidity" in m:
        return "lp_remove"
    if "staking" in m or script_kind == "staking_order":
        return "staking"
    if "limit" in m:
        return "limit"
    if m:
        return "market"
    return "unknown"


def protocol_name(msg: str, venue: str | None, script_hash: str) -> str:
    m = msg.lower()
    agg = next((a for a in ("dexhunter", "muesliswap", "minswap aggregator") if a in m), "")
    if "via muesliswap" in m:
        agg = "muesliswap"
    if "aggregator" in m and "minswap" in m:
        agg = "minswap-aggregator"
    v = venue or f"script:{script_hash[:8]}"
    return f"{v} (via {agg})" if agg and agg.split("-")[0] not in v else v


# ---- datum summary -------------------------------------------------------------------------------

def summarize_datum(value: Any, symbols: dict[str, str]) -> str:
    """Generic Plutus-data summary: referenced assets (policy+name) and integer fields, in order."""
    assets: list[str] = []
    ints: list[int] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if "int" in v and isinstance(v["int"], int):
                ints.append(v["int"])
            fields = v.get("fields") or v.get("list")
            if isinstance(fields, list):
                for i, f in enumerate(fields):
                    nxt = fields[i + 1] if i + 1 < len(fields) else None
                    if isinstance(f, dict) and isinstance(f.get("bytes"), str) and len(f["bytes"]) == 56 \
                            and isinstance(nxt, dict) and isinstance(nxt.get("bytes"), str) and len(nxt["bytes"]) <= 64:
                        key = f"{f['bytes']}.{nxt['bytes']}"
                        assets.append(symbols.get(key) or _ascii(nxt["bytes"]) or key[:12])
                    walk(f)
            for k in ("map",):
                for kv in v.get(k) or []:
                    walk(kv.get("k"))
                    walk(kv.get("v"))
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    if not assets and not ints:
        return ""
    parts = []
    if assets:
        parts.append("assets=" + ",".join(dict.fromkeys(assets)))
    if ints:
        parts.append("ints=" + ",".join(str(i) for i in ints[:8]))
    return " ".join(parts)[:240]


def _ascii(h: str) -> str:
    try:
        s = bytes.fromhex(h).decode()
        return s if s.isprintable() else ""
    except (ValueError, UnicodeDecodeError):
        return ""


# ---- classification ------------------------------------------------------------------------------

@dataclass
class CardanoState:
    scripts: dict[str, ScriptInfo]
    orders: dict[str, Order] = field(default_factory=dict)  # utxo ref -> order (latest UTxO of each order)
    all_orders: list[Order] = field(default_factory=list)
    lp_assets: set[str] = field(default_factory=set)
    assets: dict[str, tuple[str, int]] = field(default_factory=dict)  # asset -> (symbol, decimals)
    unknown_scripts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    reward_accounts: set[str] = field(default_factory=set)  # stake keys whose per-epoch rewards are booked


def classify_reward_account(stake: str, moves: list[Movement], txs: dict[str, TxContext]) -> list[Classified]:
    """Reward-account ledger: per-epoch rewards are the income; withdrawals move them into the wallet."""
    out: list[Classified] = []
    for m in sorted(moves, key=lambda m: (m.timestamp, m.tx_hash)):
        tx = txs[m.tx_hash]
        if m.source == "staking_reward":
            pool = tx.meta.get("pool") or "pool"
            out.append(_row(m, tx, Cls.OTHER, "staking_reward",
                            f"epoch {tx.meta.get('epoch')} {tx.meta.get('reward_type', 'member')} reward from {pool[:16]}… "
                            f"credited to the reward account at the start of epoch {tx.meta.get('spendable_epoch')}",
                            protocol="cardano-staking", role="in"))
        else:
            out.append(_row(m, tx, Cls.INTERNAL_TRANSFER, "reward_withdrawal",
                            "rewards withdrawn from the reward account into the wallet", protocol="cardano-staking",
                            role="out"))
    return out


def classify_cardano_wallet(wallet: str, moves: list[Movement], txs: dict[str, TxContext], ctx: RuleContext,
                            state: CardanoState) -> list[Classified]:
    groups: dict[str, list[Movement]] = defaultdict(list)
    for m in moves:
        groups[m.tx_hash].append(m)
    for h, t in txs.items():
        if t.tx_from == wallet and h not in groups:
            groups[h] = []
    symbols = {m.asset_id: m.symbol for m in moves}
    state.assets.update({m.asset_id: (m.symbol, m.decimals) for m in moves})
    rows: list[Classified] = []
    for t in sorted(txs.values(), key=lambda t: (t.timestamp, t.block, t.tx_hash)):
        h = t.tx_hash
        spends_ours = any(r in state.orders and state.orders[r].wallet == wallet for r in t.meta.get("inputs", []))
        if h in groups or spends_ours:
            rows.extend(_classify_tx(wallet, t, groups.get(h, []), ctx, state, symbols))
    return rows


def _row(m: Movement, tx: TxContext, cls: Cls, subtype: str, reason: str, *, protocol: str = "", conf: float = 1.0,
         role: str = "", linked: str = "", datum: str = "") -> Classified:
    r = Classified(m, tx, cls, subtype, protocol, conf, reason, role, linked)
    r.datum_summary = datum
    return r


def _classify_tx(wallet: str, tx: TxContext, legs: list[Movement], ctx: RuleContext, state: CardanoState,
                 symbols: dict[str, str]) -> list[Classified]:
    sent = tx.tx_from == wallet
    msg = " | ".join(str(x) for x in tx.meta.get("msg", []))
    out: list[Classified] = []
    rest: list[Movement] = []

    # staking legs
    for m in legs:
        if m.counterparty == REWARDS_CP and wallet in state.reward_accounts:
            out.append(_row(m, tx, Cls.INTERNAL_TRANSFER, "reward_withdrawal",
                            "rewards withdrawn from this stake key's reward account (income booked per epoch)",
                            protocol="cardano-staking", role="in"))
        elif m.counterparty == REWARDS_CP:
            out.append(_row(m, tx, Cls.OTHER, "staking_reward_withdrawal", "reward withdrawal from the stake account",
                            protocol="cardano-staking"))
        elif m.counterparty == DEPOSIT_CP:
            sub = "stake_registration" if m.amount < 0 else "stake_deregistration"
            out.append(_row(m, tx, Cls.OTHER, sub, f"{'2 ADA key deposit paid' if m.amount < 0 else 'key deposit refunded'}",
                            protocol="cardano-staking"))
        else:
            rest.append(m)

    script_outputs: dict[str, Any] = tx.meta.get("script_outputs", {})

    # 1) settlement of our earlier orders
    spent = [state.orders[r] for r in tx.meta.get("inputs", []) if r in state.orders and state.orders[r].wallet == wallet]
    def is_order_output(ref: str) -> bool:
        so = script_outputs.get(ref)
        return bool(so and (so.get("datum_hash") or so.get("datum")))

    placement_legs = [m for m in rest if sent and m.amount < 0 and m.utxo_ref
                      and any(is_order_output(r) for r in m.utxo_ref.split(";"))]
    if spent:
        settle = [m for m in rest if m not in placement_legs]
        out.extend(_settle(wallet, tx, settle, spent, sent, state, script_outputs))
        rest = [m for m in rest if m in placement_legs]

    # 2) new order placements
    if placement_legs:
        out.extend(_place(wallet, tx, rest, placement_legs, msg, state, symbols))
        rest = []

    # 3) everything else: generic rules, with mint / min-ADA conventions
    if rest or (sent and not out):
        mint = [m for m in rest if m.counterparty == MINT_CP]
        other = [m for m in rest if m.counterparty != MINT_CP]
        if other or not mint:
            g = classify_group(wallet, tx, other, ctx)
            if "reward" in msg.lower() and other and all(m.amount > 0 for m in other):
                for r in g:
                    r.classification, r.subtype = Cls.OTHER, "protocol_reward"
                    r.reason = f"reward payout ({msg})"
            if not other and g and g[0].movement.source == "gas":
                g[0].subtype = _fee_only_subtype(tx)
                g[0].reason = f"wallet-signed tx with no external movement ({g[0].subtype})"
            out.extend(g)
        for m in mint:
            out.append(_row(m, tx, Cls.OTHER, "mint" if m.amount > 0 else "burn",
                            "token minted/burned in a wallet-signed tx", protocol=msg))
        for r in out:
            if r.movement.source == "min_ada":
                r.leg_role = "min_ada"
    for r in out:
        if r.movement.utxo_ref:
            for ref in r.movement.utxo_ref.split(";"):
                so = script_outputs.get(ref)
                if so and so["script_hash"] not in state.scripts:
                    state.unknown_scripts[so["script_hash"]] += 1
    return _with_gas(out, wallet, tx) if out else out


def _fee_only_subtype(tx: TxContext) -> str:
    certs = [c.get("type") for c in tx.meta.get("certs", [])]
    if any(c and "deleg" in c for c in certs):
        return "delegation"
    if certs:
        return "certificate:" + ",".join(str(c) for c in certs)
    return "self_send"


def _place(wallet: str, tx: TxContext, legs: list[Movement], order_legs: list[Movement], msg: str,
           state: CardanoState, symbols: dict[str, str]) -> list[Classified]:
    script_outputs: dict[str, Any] = tx.meta["script_outputs"]
    rows: list[Classified] = []
    new_orders: list[Order] = []
    for ref in dict.fromkeys(r for m in order_legs for r in m.utxo_ref.split(";") if r in script_outputs):
        so = script_outputs[ref]
        info = state.scripts.get(so["script_hash"])
        sold = {m.asset_id for m in order_legs if ref in m.utxo_ref.split(";") and m.source != "min_ada"}
        kind = order_kind(msg, info.kind if info else None)
        if not kind == "lp_remove" and sold & state.lp_assets:
            kind = "lp_remove"
        o = Order(ref=ref, wallet=wallet, placed_tx=tx.tx_hash, script_hash=so["script_hash"],
                  venue=protocol_name(msg, info.venue if info else None, so["script_hash"]), kind=kind, sold=sold,
                  datum_summary=summarize_datum(so.get("datum"), symbols), registered=info is not None,
                  terms=decode(info.venue, so.get("datum")) if info else None, order_id=f"cardano:{ref}")
        for m in order_legs:
            if ref in m.utxo_ref.split(";") and m.source != "min_ada":
                o.sold_amt[m.asset_id] = o.sold_amt.get(m.asset_id, Decimal(0)) - m.amount
        state.orders[ref] = o
        state.all_orders.append(o)
        new_orders.append(o)
    by_ref = {o.ref: o for o in new_orders}
    for m in legs:
        hit = next((by_ref[r] for r in m.utxo_ref.split(";") if r in by_ref), None)
        if m.amount > 0:
            rows.append(_row(m, tx, Cls.OTHER, "order_change", "inflow in an order-placement tx", protocol=msg))
            continue
        main = hit or new_orders[0]
        role = "min_ada" if m.source == "min_ada" else ("order" if hit else "fee")
        r = _row(m, tx, Cls.ORDER_PLACED, main.kind, f"funds sent to {main.venue} order script "
                 f"{main.script_hash[:8]} with datum ({msg or 'no CIP-20 msg'})",
                 protocol=main.venue, conf=1.0 if main.registered else 0.9, role=role, datum=main.datum_summary)
        main.rows.append(r)
        rows.append(r)
    return rows


def _settle(wallet: str, tx: TxContext, legs: list[Movement], spent: list[Order], sent: bool, state: CardanoState,
            script_outputs: dict[str, Any]) -> list[Classified]:
    sold = set().union(*(o.sold for o in spent))
    o0 = spent[0]
    received = {m.asset_id for m in legs if m.amount > 0 and m.source != "min_ada"}
    tokens_in = received - {NATIVE_ASSET}
    sold_tokens = sold - {NATIVE_ASSET}
    continuation = [r for r, so in script_outputs.items() if any(so["script_hash"] == o.script_hash for o in spent)]
    conf = 1.0 if o0.registered else 0.9

    msg = " | ".join(str(x) for x in tx.meta.get("msg", [])).lower()
    minted: dict[str, int] = defaultdict(int)
    for mm in tx.meta.get("minted", []):
        minted[mm["asset"]] += int(mm["qty"])
    if continuation and not legs and not sent:
        cls, sub, outcome = Cls.ORDER_FILLED, "partial", "partial"
        reason = "partial match: order UTxO consumed and re-created at the same script (no payout yet)"
    elif o0.kind == "staking" and "cancel" not in msg:
        cls, sub, outcome = Cls.OTHER, "staking_order_settled", "settled"
        reason = "staking order UTxO spent (stake update / unstake)"
    elif sent or "cancel" in msg:
        cls, sub, outcome = Cls.ORDER_CANCELLED, "cancel", "cancelled"
        reason = "order UTxO spent by a cancel tx" + (" (wallet-signed)" if sent else f" ({msg})")
    elif (minted_in := {a for a in tokens_in if minted.get(a, 0) > 0}) and not sent:
        cls, sub, outcome = Cls.LP_DEPOSIT, "lp", "lp_minted"
        state.lp_assets |= minted_in
        reason = "order settled by minting the LP token received (" + ",".join(
            next((m.symbol for m in legs if m.asset_id == a), a[:12]) for a in minted_in) + ")"
    elif (burned := {a for a in sold_tokens if minted.get(a, 0) < 0}) and not sent:
        cls, sub, outcome = Cls.LP_WITHDRAW, "lp", "lp_redeemed"
        state.lp_assets |= burned
        reason = "order settled by burning the LP token sold; underlying received"
    elif o0.kind == "lp_add" and (tokens_in - sold):
        cls, sub, outcome = Cls.LP_DEPOSIT, "lp", "lp_minted"
        state.lp_assets |= tokens_in - sold
        reason = "batcher executed LP add order; LP token received"
    elif o0.kind == "lp_remove" and received - sold:
        cls, sub, outcome = Cls.LP_WITHDRAW, "lp", "lp_redeemed"
        reason = "batcher executed LP remove order; underlying received"
    elif (sold_tokens and sold_tokens & tokens_in and not (tokens_in - sold_tokens)) or \
            (not sold_tokens and not tokens_in):
        cls, sub, outcome = Cls.ORDER_CANCELLED, "refund", "refunded"
        reason = "order UTxO spent by batcher/protocol and the sold asset was returned (refund/expiry)"
    else:
        partial = bool(continuation) or bool(sold_tokens & tokens_in)
        cls, sub, outcome = Cls.ORDER_FILLED, "partial" if partial else o0.kind, "partial" if partial else "filled"
        reason = f"batcher spent order UTxO {o0.ref[:12]}… and paid out " + ",".join(sorted(
            m.symbol for m in legs if m.amount > 0 and m.source != "min_ada"))
    rows: list[Classified] = []
    got: dict[str, Decimal] = defaultdict(Decimal)
    for m in legs:
        if m.amount > 0:
            got[m.asset_id] += m.amount
    for o in spent:
        o.fills.append((tx.tx_hash, dict(got)))
    for m in legs:
        role = "min_ada" if m.source == "min_ada" else ("in" if m.amount > 0 else "fee")
        r = _row(m, tx, cls, sub, f"{reason}; placed in {o0.placed_tx[:10]}", protocol=o0.venue, conf=conf,
                 role=role, linked=o0.placed_tx, datum=o0.datum_summary)
        r.order_id = o0.order_id
        o0.settle_rows.append(r)
        rows.append(r)
    if not legs and not sent:
        pass  # settlement paid elsewhere (e.g. to a different address); nothing for this wallet
    for o in spent:
        state.orders.pop(o.ref, None)
        o.settled_tx = o.settled_tx or tx.tx_hash
        o.outcome = outcome if not o.outcome else f"{o.outcome}+{outcome}"
        for r in o.rows:
            r.linked_tx_hash = r.linked_tx_hash or tx.tx_hash
    if continuation and not sent and outcome == "partial":
        for ref in continuation:
            nxt = Order(ref=ref, wallet=wallet, placed_tx=o0.placed_tx, script_hash=o0.script_hash, venue=o0.venue,
                        kind=o0.kind, sold=o0.sold, datum_summary=o0.datum_summary, registered=o0.registered,
                        rows=o0.rows, terms=o0.terms, sold_amt=o0.sold_amt, fills=o0.fills,
                        settle_rows=o0.settle_rows, order_id=o0.order_id)
            state.orders[ref] = nxt
            state.all_orders.append(nxt)
    return rows


def finalize_orders(state: CardanoState, spent_status: Callable[[list[str]], dict[str, bool]]) -> None:
    """Placement subtypes get their outcome; never-settled orders are checked on chain (open vs spent elsewhere)."""
    open_refs = [o.ref for o in state.orders.values()]
    status = spent_status(open_refs) if open_refs else {}
    for o in state.all_orders:
        if not o.outcome:
            o.outcome = "spent_outside_history" if status.get(o.ref) else "open"
    by_rows: dict[int, list[Order]] = defaultdict(list)
    for o in state.all_orders:
        by_rows[id(o.rows)].append(o)
    for orders in by_rows.values():
        outcomes = [o.outcome for o in orders]
        final = outcomes[-1] if outcomes else ""
        if any("partial" in x for x in outcomes) and final not in ("open",):
            final = "partial→" + final
        for r in orders[0].rows:
            r.subtype = f"{orders[0].kind}:{final}"
            r.order_id = orders[0].order_id
        _describe(orders[0], final, state.assets)


def _fmt(raw_or_dec: Decimal, asset: str | None, assets: dict[str, tuple[str, int]], *, raw: bool) -> str:
    sym, dec = assets.get(asset or "", ("?", 0)) if asset != NATIVE_ASSET else ("ADA", 6)
    v = raw_or_dec.scaleb(-dec) if raw else raw_or_dec
    return f"{v.normalize():f} {sym}"


def _price(sell_asset: str, sell: Decimal, buy_asset: str, buy: Decimal, assets: dict[str, tuple[str, int]]) -> str:
    """Human price; quoted as ADA per token when one side is ADA."""
    if not sell or not buy:
        return ""

    def sym(a: str) -> str:
        return "ADA" if a == NATIVE_ASSET else assets.get(a, ("?", 0))[0]
    if sell_asset == NATIVE_ASSET:
        return f"{(sell / buy):.8g} ADA/{sym(buy_asset)}"
    if buy_asset == NATIVE_ASSET:
        return f"{(buy / sell):.8g} ADA/{sym(sell_asset)}"
    return f"{(buy / sell):.8g} {sym(buy_asset)}/{sym(sell_asset)}"


def _describe(o: Order, outcome: str, assets: dict[str, tuple[str, int]]) -> None:
    """order_details for placement rows (terms) and settlement rows (execution vs limit)."""
    t = o.terms
    # sold asset/amount: tokens from the legs are exact; for ADA prefer the datum's explicit amount
    tokens = {a: v for a, v in o.sold_amt.items() if a != NATIVE_ASSET}
    if tokens:
        sell_asset, sell = next(iter(tokens.items()))
        approx = False
    else:
        sell_asset = NATIVE_ASSET
        if t and t.sell_amount:
            sell, approx = Decimal(t.sell_amount).scaleb(-6), False
        else:
            sell = o.sold_amt.get(NATIVE_ASSET, Decimal(0)) - (Decimal(t.fee).scaleb(-6) if t and t.fee else Decimal(0))
            approx = True
    buy_asset = t.buy_asset if t and t.buy_asset else None
    if buy_asset is None:  # implied by the venue (VyFi, Minswap V2): take it from what was received
        for _, got in o.fills:
            cand = [a for a in got if a != sell_asset and a not in o.sold]
            if cand:
                buy_asset = max(cand, key=lambda a: got[a] if a != NATIVE_ASSET else Decimal(0)) if len(cand) > 1 else cand[0]
                break
        if buy_asset is None and sell_asset != NATIVE_ASSET:
            buy_asset = NATIVE_ASSET
    parts = [f"sell {'≈' if approx else ''}{_fmt(sell, sell_asset, assets, raw=False)}"]
    limit = ""
    if t and t.min_receive is not None:
        dec = 6 if buy_asset == NATIVE_ASSET else assets.get(buy_asset or "", ("?", 0))[1]
        min_recv = Decimal(t.min_receive).scaleb(-dec)
        parts.append(f"for ≥{_fmt(min_recv, buy_asset, assets, raw=False)}" if buy_asset else f"min_receive_raw={t.min_receive}")
        if buy_asset:
            limit = _price(sell_asset, sell, buy_asset, min_recv, assets)
            parts.append(f"limit {limit}")
    if t and t.expiry_ms:
        from datetime import UTC, datetime
        parts.append("expires " + datetime.fromtimestamp(t.expiry_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M"))
    if t and t.allow_partial is not None:
        parts.append("partial fills " + ("allowed" if t.allow_partial else "not allowed"))
    parts.append(f"outcome {outcome}")
    terms = "; ".join(parts)
    for r in o.rows:
        r.order_details = terms
    if buy_asset:
        recv = sum((got.get(buy_asset, Decimal(0)) for _, got in o.fills), Decimal(0))
        back = sum((got.get(sell_asset, Decimal(0)) for _, got in o.fills), Decimal(0))
        if sell_asset == NATIVE_ASSET:  # only ADA beyond the order's fees/deposit counts as a refund
            back = max(Decimal(0), back - (o.sold_amt.get(NATIVE_ASSET, Decimal(0)) - sell))
        sold_eff = sell - back
        if recv and sold_eff > 0:
            exe = _price(sell_asset, sold_eff, buy_asset, recv, assets)
            note = f"received {_fmt(recv, buy_asset, assets, raw=False)} for {_fmt(sold_eff, sell_asset, assets, raw=False)}" \
                   f"; executed {exe}" + (f" vs limit {limit}" if limit else "")
            if buy_asset == NATIVE_ASSET:
                note += " (ADA received includes returned deposit)"
            if approx:
                note = note.replace("executed ", "executed ≈") + " (ADA sold is estimated: order ADA minus refunds, so it " \
                       "includes matcher fees; small partial fills look worse than the limit)"
            for r in o.settle_rows:
                r.order_details = note
