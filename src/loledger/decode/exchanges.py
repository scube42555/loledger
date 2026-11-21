"""Exchange-account classification and exact linking of exchange deposits/withdrawals to wallet txs.

Rows from an exchange ledger (asset_kind=exchange):
  order (fills + fees)           -> SWAP/exchange_trade (fee legs leg_role=fee)
  on-chain deposit / withdrawal  -> TRANSFER/deposit_from_chain | withdrawal_to_chain, then
  linked by tx hash to the wallet leg on the other chain (log index / vout when the exchange gives
  one) -> both sides INTERNAL_TRANSFER (confidence 1.0) when the wallet is one of ours.
Unlinked dust deposits (< DUST units) are flagged OTHER/spam_dust_deposit (address poisoning of the
exchange deposit address).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from functools import cache
from importlib import resources

import yaml

from ..model import NATIVE_ASSET, AssetKind, Movement, TxContext
from ..registry import norm_symbol
from ..rules import Classified, Cls

DUST = Decimal("0.01")


@cache
def exchange_price_ids() -> dict[str, str]:
    raw = yaml.safe_load(resources.files("loledger.registry").joinpath("exchanges.yaml").read_text()) or {}
    return {str(k).upper(): str(v) for k, v in (raw.get("price_ids") or {}).items()}


def classify_exchange(moves: list[Movement], txs: dict[str, TxContext], venue: str,
                      credit_subtype: str = "") -> list[Classified]:
    groups: dict[str, list[Movement]] = defaultdict(list)
    for m in moves:
        groups[m.tx_hash].append(m)
    rows: list[Classified] = []
    for h in sorted(groups, key=lambda h: txs[h].timestamp):
        t, legs = txs[h], groups[h]
        jt = str(t.meta.get("journal_type", t.function_name))
        if jt in ("TRADING", "TRADE_FEE", "trade"):
            net: dict[str, Decimal] = defaultdict(Decimal)
            for m in legs:
                if m.source != "fee":
                    net[m.symbol] += m.amount
            sold = {k: -v for k, v in net.items() if v < 0}
            bought = {k: v for k, v in net.items() if v > 0}
            fills = t.meta.get("fills", "?")
            details = ""
            if len(sold) == 1 and len(bought) == 1:
                (sa, sv), (ba, bv) = next(iter(sold.items())), next(iter(bought.items()))
                quote_first = sa in ("USD", "USDT", "USDC")
                price = (sv / bv) if quote_first else (bv / sv)
                unit = f"{sa}/{ba}" if quote_first else f"{ba}/{sa}"
                details = f"sold {sv.normalize():f} {sa} for {bv.normalize():f} {ba}; avg price {price:.8g} {unit}; {fills} fill(s)"
            for m in legs:
                role = "fee" if m.source == "fee" else ("in" if m.amount > 0 else "out")
                r = Classified(m, t, Cls.SWAP, "exchange_trade", venue, 1.0,
                               f"{venue} order {h.split(':')[-1]} ({t.meta.get('instrument', '')})", role)
                r.order_id, r.order_details = h, details
                rows.append(r)
        elif "deposit" in jt.lower() and not jt.startswith("FIAT_"):
            for m in legs:
                rows.append(Classified(m, t, Cls.TRANSFER, "deposit_from_chain", venue, 1.0,
                                       f"on-chain deposit to {venue} ({t.meta.get('network', '?')} "
                                       f"tx {str(t.meta.get('txid', '?'))[:14]}…)", "in"))
        elif "withdraw" in jt.lower() and not jt.startswith("FIAT_"):
            for m in legs:
                if m.source == "fee":
                    rows.append(Classified(m, t, Cls.OTHER, "exchange_withdrawal_fee", venue, 1.0,
                                           f"{venue} network fee for withdrawal", "fee"))
                    continue
                rows.append(Classified(m, t, Cls.TRANSFER, "withdrawal_to_chain", venue, 1.0,
                                       f"on-chain withdrawal from {venue} ({t.meta.get('network', '?')} "
                                       f"tx {str(t.meta.get('txid', '?'))[:14]}…, fee {t.meta.get('fee', '?')})", "out"))
        elif jt == "INTERNAL_MOVE":
            for m in legs:
                rows.append(Classified(m, t, Cls.INTERNAL_TRANSFER, "exchange_subaccount", venue, 1.0,
                                       f"{venue} move {t.meta.get('from')} → {t.meta.get('to')} (spot ↔ futures)",
                                       "in" if m.amount > 0 else "out"))
        elif jt == "FUTURES_PNL":
            for m in legs:
                fee = m.source == "fee"
                rows.append(Classified(m, t, Cls.OTHER, "futures_fee" if fee else "futures_realized_pnl", venue, 1.0,
                                       f"{venue} futures {t.meta.get('instrument')} {t.meta.get('side')} "
                                       f"(vol {t.meta.get('deal_vol')} @ {t.meta.get('deal_avg_price')}, "
                                       f"leverage {t.meta.get('leverage')})", "fee" if fee else ("in" if m.amount > 0 else "out")))
        elif jt == "EXCHANGE_CREDIT":
            for m in legs:
                if credit_subtype:
                    rows.append(Classified(m, t, Cls.OTHER, credit_subtype, venue, 1.0,
                                           f"{venue} credit with no on-chain tx ('{t.meta.get('spec')}'); "
                                           f"user-confirmed {credit_subtype.replace('_', ' ')}", "in"))
                else:
                    rows.append(Classified(m, t, Cls.OTHER, "exchange_credit", venue, 0.8,
                                           f"{venue} credit with no on-chain tx ('{t.meta.get('spec')}'): an internal "
                                           "credit, likely a staking/earn reward", "in"))
        elif jt.startswith("FIAT_"):
            for m in legs:
                fee = m.source == "fee"
                sub = "exchange_fee" if fee else ("fiat_deposit" if "DEPOSIT" in jt else "fiat_withdrawal")
                rows.append(Classified(m, t, Cls.TRANSFER if not fee else Cls.OTHER, sub, venue, 1.0,
                                       f"{venue} {t.meta.get('spec', jt.lower())}", "fee" if fee else ("in" if m.amount > 0 else "out")))
        else:
            for m in legs:
                rows.append(Classified(m, t, Cls.OTHER, jt.lower(), venue, 0.9, f"{venue} ledger entry {jt} "
                                       f"{t.meta.get('spec', '')}", "in" if m.amount > 0 else "out"))
    return rows


def _norm_txid(t: str) -> str:
    t = (t or "").strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    return t.lower() if all(c in "0123456789abcdefABCDEF" for c in t) else t  # base58 stays case-sensitive


def link_exchange_transfers(rows: list[Classified]) -> list[str]:
    """Exact link exchange deposit/withdrawal <-> wallet leg (or <-> another exchange) by tx hash."""
    notes_x: list[str] = []
    by_txid: dict[str, list[Classified]] = defaultdict(list)
    for r in rows:
        if r.movement.asset_kind == AssetKind.EXCHANGE and r.subtype in ("deposit_from_chain", "withdrawal_to_chain") \
                and r.leg_role != "fee" and _norm_txid(str(r.tx.meta.get("txid") or "")):
            by_txid[_norm_txid(str(r.tx.meta.get("txid")))].append(r)
    for txid, group in by_txid.items():
        deps = [g for g in group if g.subtype == "deposit_from_chain"]
        wds = [g for g in group if g.subtype == "withdrawal_to_chain"]
        if len(deps) == 1 and len(wds) == 1 and deps[0].movement.chain != wds[0].movement.chain \
                and norm_symbol(deps[0].movement.symbol) == norm_symbol(wds[0].movement.symbol):
            d, w = deps[0], wds[0]
            d.classification = w.classification = Cls.INTERNAL_TRANSFER
            d.subtype, w.subtype = f"from_exchange:{w.protocol}", f"to_exchange:{d.protocol}"
            d.linked_tx_hash, w.linked_tx_hash = w.tx.tx_hash, d.tx.tx_hash
            d.reason += f"; same on-chain tx hash as the {w.protocol} withdrawal (exchange-to-exchange)"
            w.reason += f"; same on-chain tx hash as the {d.protocol} deposit (exchange-to-exchange)"
            notes_x.append(f"{w.protocol} → {d.protocol}: {abs(d.movement.amount).normalize():f} {d.movement.symbol} "
                           f"(exchange-to-exchange, same tx hash)")
    onchain: dict[str, list[Classified]] = defaultdict(list)
    for r in rows:
        if r.movement.asset_kind != AssetKind.EXCHANGE:
            onchain[r.tx.tx_hash.lower()].append(r)
            if r.movement.leg_id.startswith("coin:"):  # Chia: exchanges report the output coin id
                onchain[r.movement.leg_id[5:].lower()].append(r)
    notes: list[str] = notes_x
    for r in rows:
        if r.movement.asset_kind != AssetKind.EXCHANGE or r.subtype not in ("deposit_from_chain", "withdrawal_to_chain"):
            continue
        txid = str(r.tx.meta.get("txid") or "").lower()
        deposit = r.subtype == "deposit_from_chain"
        fam = norm_symbol(r.movement.symbol)
        cands = [c for c in onchain.get(txid, []) + onchain.get("0x" + txid, [])
                 if (c.movement.amount < 0 if deposit else c.movement.amount > 0)
                 and (norm_symbol(c.movement.symbol) == fam or (c.movement.asset_id == NATIVE_ASSET and fam in ("eth", "avax", "ada")))]
        idx = str(r.tx.meta.get("txid_index") or "")
        exact = [c for c in cands if idx and c.movement.leg_id.split("#")[0] in (f"log:{idx}", f"vout:{idx}")]
        pick = exact[0] if exact else (min(cands, key=lambda c: abs(abs(c.movement.amount) - abs(r.movement.amount))) if cands else None)
        if pick is None:
            if deposit and txid and abs(r.movement.amount) < DUST and \
                    norm_symbol(r.movement.symbol) in ("usdt", "usdc", "dai", "usd"):
                r.classification, r.subtype, r.confidence = Cls.OTHER, "spam_dust_deposit", 0.9
                r.reason += "; dust deposit from an address not in the wallet list (likely address poisoning)"
            else:
                r.reason += "; counterpart wallet not in the address list" if txid else "; no tx id from the exchange"
            continue
        venue = r.protocol
        side_x, side_w = ("from_wallet", f"to_exchange:{venue}") if deposit else ("to_wallet", f"from_exchange:{venue}")
        for a, b, sub in ((r, pick, side_x), (pick, r, side_w)):
            a.classification, a.subtype, a.confidence = Cls.INTERNAL_TRANSFER, sub, 1.0
            a.linked_tx_hash = b.tx.tx_hash
        r.reason += f"; matched {pick.movement.chain} wallet {pick.movement.wallet[:12]}… tx {pick.tx.tx_hash[:14]}… " \
                    f"({'log index' if exact else 'tx hash'} match)"
        pick.reason = f"{'deposit to' if deposit else 'withdrawal from'} {venue} (exchange ledger {r.tx.tx_hash[:30]}…, exact tx-hash match)"
        notes.append(f"{venue} {'deposit' if deposit else 'withdrawal'} {abs(r.movement.amount).normalize():f} {r.movement.symbol} "
                     f"↔ {pick.movement.chain} {pick.movement.wallet[:10]}… tx {pick.tx.tx_hash[:14]}…")
    return notes


# ---- linking without tx hashes -------------------------------------------------------------------

DEPOSIT_TOL = Decimal("0.01")    # exchange credits the sent amount (±1%)
WITHDRAW_TOL = Decimal("0.05")   # wallet receives the exchange amount minus a network fee (≤5%)
MAX_GAP = 24 * 3600              # local-time exports (Coinbase) can be hours off
# flat network-fee allowance per asset family for small withdrawals (in addition to the % tolerance)
FLAT_FEE = {"usdt": Decimal("1.5"), "usdc": Decimal("1.5"), "ada": Decimal("1.5"), "eth": Decimal("0.005"),
            "btc": Decimal("0.0005"), "sol": Decimal("0.02"), "avax": Decimal("0.1"), "doge": Decimal("5"),
            "night": Decimal("5"), "link": Decimal("0.5")}


def _unlinked_exchange(r: Classified) -> bool:
    return (r.movement.asset_kind == AssetKind.EXCHANGE and not r.linked_tx_hash
            and r.subtype in ("deposit_from_chain", "withdrawal_to_chain") and r.leg_role != "fee")


def link_exchange_heuristic(rows: list[Classified], deposit_addresses: dict[str, set[str]],
                            labels: dict[str, str]) -> list[str]:
    """Link exchange deposits/withdrawals that carry no tx hash (OKX, Binance.US, Coinbase) to wallet legs
    or to another exchange's withdrawal, scored on amount, time gap and hints (known deposit address,
    wallet label). Greedy one-to-one; confidence 0.6–0.95. Also: wallet sends to a configured exchange
    deposit address are the user's own transfers (INTERNAL_TRANSFER) even without an exchange-side row."""
    import math

    notes: list[str] = []
    addr_venue = {a: v for v, addrs in deposit_addresses.items() for a in addrs}
    label_wallet = {lab.strip().lower(): w.rsplit("|", 1)[-1] for w, lab in labels.items() if lab}
    xs = [r for r in rows if _unlinked_exchange(r)]
    chain_cands = [r for r in rows if r.movement.asset_kind != AssetKind.EXCHANGE and not r.linked_tx_hash
                   and r.classification in (Cls.TRANSFER, Cls.OTHER) and r.leg_role not in ("fee", "min_ada")
                   and not r.subtype.startswith("spam") and r.movement.amount != 0]
    pairs: list[tuple[float, Classified, Classified, str]] = []
    for x in xs:
        deposit = x.subtype == "deposit_from_chain"
        fam = norm_symbol(x.movement.symbol)
        amt = abs(x.movement.amount)
        want_label = str(x.tx.meta.get("wallet_label") or "").strip().lower()
        dest = str(x.tx.meta.get("address") or "").strip().lower()
        has_txid = bool(str(x.tx.meta.get("txid") or "").strip())
        pool = [c for c in chain_cands if (c.movement.amount < 0) == deposit]
        if not deposit and dest:
            # the exchange names the destination: only legs in that exact wallet qualify
            pool = [c for c in pool if c.movement.wallet == dest]
        elif has_txid:
            pool = []  # a tx hash we could not find contradicts any other wallet tx
        # exchange -> exchange (e.g. Gemini withdrawal to the user's OKX deposit address)
        pool += [y for y in xs if y is not x and y.subtype != x.subtype and y.movement.chain != x.movement.chain]
        for c in pool:
            if norm_symbol(c.movement.symbol) != fam:
                continue
            camt = abs(c.movement.amount)
            sent, recv = (camt, amt) if deposit else (amt, camt)  # recv <= sent (fees)
            if recv > sent * (1 + Decimal("0.0001")):
                continue
            diff = (sent - recv) / sent if sent else Decimal(1)
            tol = DEPOSIT_TOL if deposit else WITHDRAW_TOL
            allowed = max(tol * sent, Decimal(0) if deposit else FLAT_FEE.get(fam, Decimal(0)))
            if sent - recv > allowed:
                continue
            ratio = (sent - recv) / allowed if allowed else Decimal(0)
            gap = (x.tx.timestamp - c.tx.timestamp) if deposit else (c.tx.timestamp - x.tx.timestamp)
            early = 14 * 3600 if (x.tx.meta.get("time_zone_unknown") or c.tx.meta.get("time_zone_unknown")) else 3600
            if not -early <= gap <= MAX_GAP:
                continue
            hints = []
            cp = c.movement.counterparty if c.movement.asset_kind != AssetKind.EXCHANGE else str(c.tx.meta.get("address") or "").lower()
            if addr_venue.get(cp) == x.movement.chain.lower():
                hints.append("known deposit address")
            if want_label and label_wallet.get(want_label) == c.movement.wallet:
                hints.append(f"wallet label '{want_label}'")
            if not deposit and dest and c.movement.wallet == dest:
                hints.append("exchange-reported destination = this wallet")
            amount_score = 1 - float(ratio)
            time_score = math.exp(-abs(gap) / (6 * 3600))
            score = 0.5 * amount_score + 0.3 * time_score + 0.2 * min(1, len(hints))
            if score < 0.55:
                continue
            pairs.append((score, x, c, ", ".join(hints) + (f"; Δt {gap / 3600:.1f}h, amount diff {diff * 100:.2f}%")))
    pairs.sort(key=lambda p: -p[0])
    used: set[int] = set()
    for score, x, c, why in pairs:
        if id(x) in used or id(c) in used:
            continue
        used |= {id(x), id(c)}
        conf = round(0.6 + 0.35 * score, 2)
        deposit = x.subtype == "deposit_from_chain"
        venue = x.protocol
        c_sub = f"to_exchange:{venue}" if deposit else f"from_exchange:{venue}"
        for a, b, sub in ((x, c, "from_wallet" if deposit else "to_wallet"), (c, x, c_sub)):
            a.classification, a.subtype, a.confidence = Cls.INTERNAL_TRANSFER, sub, min(a.confidence, conf)
            a.linked_tx_hash = b.tx.tx_hash
            a.reason += f"; heuristic link ({why}, score {score:.2f})"
        notes.append(f"{venue} {'deposit' if deposit else 'withdrawal'} {abs(x.movement.amount).normalize():f} {x.movement.symbol} "
                     f"↔ {c.movement.chain} {c.movement.wallet[:12]}… ({why}; conf {conf})")
    # wallet sends to a configured exchange deposit address, still unmatched: own transfer by address
    for c in chain_cands:
        v = addr_venue.get(c.movement.counterparty)
        if v and not c.linked_tx_hash and c.movement.amount < 0:
            c.classification, c.subtype, c.confidence = Cls.INTERNAL_TRANSFER, f"to_exchange:{v}", 1.0
            c.reason += f"; sent to the user's {v} deposit address (no matching {v} ledger row in the export)"
            notes.append(f"{v} deposit address: {abs(c.movement.amount).normalize():f} {c.movement.symbol} from "
                         f"{c.movement.chain} {c.movement.wallet[:12]}… (address match only)")
    return notes
