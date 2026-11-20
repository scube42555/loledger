"""Custody reconciliation over classified.csv: are all assets accounted for?

The wallet list + exchange accounts are meant to be *everything* the user holds, so:

1. Holdings: each (chain, wallet) ending balance from classified rows (gas included, fake tokens
   excluded) vs the live balance snapshot written by `classify verify`.
2. Gaps: a running balance that goes negative means an inflow is missing (or arrives out of order).
3. Boundary flows: value entering from, or leaving to, an address outside the list. Each needs a
   story for cost basis (income? purchase? own unlisted wallet?) or disposal (sale? gift? payment?).
   Outflows are paired with later inflows of the same asset elsewhere (a hop through an unlisted
   address), or with same-time inflows of another asset (an exchanger / instant swap service).

Spam legs are dropped only when the token is fake (never priced and not native): poisoning dust of
the real USDT or SOL did arrive, and is later swept out with the rest of the balance.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby
from pathlib import Path
from typing import Any

from .registry import norm_symbol

SAME_ASSET = {"wavax": "avax", "weth": "eth", "weth.e": "eth", "wsol": "sol", "wbtc": "btc", "matic": "pol",
              "usdc.e": "usdc", "msol": "msol"}
BOUNDARY_SUBTYPES = {"from_eoa", "to_eoa", "to_contract", "withdrawal_to_chain", "deposit_from_chain",
                     "bridge_in", "bridge_out", "self_or_passthrough", "contract_call", "unknown",
                     "from_exchange", "to_exchange"}
DAY = 86400


def _dec(s: str) -> Decimal:
    try:
        return Decimal(s) if s else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def family(sym: str) -> str:
    n = norm_symbol(sym or "")
    return SAME_ASSET.get(n, n)


@dataclass(slots=True)
class Row:
    chain: str
    ts: int
    tx: str
    wallet: str
    label: str
    token: str
    asset: str
    amount: Decimal
    usd: Decimal | None
    price: Decimal | None
    cls: str
    sub: str
    gas: Decimal
    counterparty: str
    linked: str
    reason: str

    @property
    def where(self) -> str:
        return self.label or self.wallet

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.ts, UTC).strftime("%Y-%m-%d %H:%M")


def load(path: Path) -> list[Row]:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Row(r["chain"], int(r["timestamp"]), r["tx_hash"], r["wallet"], r["wallet_label"], r["token"],
                           r["token_address"] or r["token"], _dec(r["amount"]),
                           _dec(r["usd_value_at_time"]) if r["usd_value_at_time"] else None,
                           _dec(r["usd_price"]) if r["usd_price"] else None, r["classification"], r["subtype"],
                           _dec(r["gas_fee_native"]), r["counterparty"], r["linked_tx_hash"], r["reason"]))
    out.sort(key=lambda r: r.ts)
    return out


def fill_labels(rows: list[Row]) -> None:
    lab = {(r.chain, r.wallet): r.label for r in rows if r.label}
    for r in rows:
        if not r.label:
            r.label = lab.get((r.chain, r.wallet)) or lab.get((r.chain, r.wallet + "#rewards"), "").replace(
                " · rewards", "") or r.wallet


def real_rows(rows: list[Row]) -> list[Row]:
    """Drop fake-token spam; keep spam legs of real (priced or native) assets."""
    priced = {(r.chain, r.asset) for r in rows if r.price is not None and not r.sub.startswith("spam")}
    return [r for r in rows if not r.sub.startswith("spam") or r.asset == "native" or (r.chain, r.asset) in priced]


@dataclass(slots=True)
class Holding:
    chain: str
    wallet: str
    where: str
    asset: str
    symbol: str
    qty: Decimal
    price: Decimal | None
    live: Decimal | None

    @property
    def status(self) -> str:
        if self.live is None:
            return "no live check"
        tol = max(Decimal("1e-9"), abs(self.live) * Decimal("1e-10"))
        return "match" if abs(self.qty - self.live) <= tol else "MISMATCH"

    @property
    def usd(self) -> Decimal | None:
        return None if self.price is None else self.qty * self.price


def _ledgers(rows: list[Row]) -> dict[tuple[str, str, str], list[tuple[int, Decimal]]]:
    """(chain, wallet, asset) -> [(ts, delta)], gas charged to the native asset."""
    out: dict[tuple[str, str, str], list[tuple[int, Decimal]]] = defaultdict(list)
    for r in rows:
        if r.amount:
            out[(r.chain, r.wallet, r.asset)].append((r.ts, r.amount))
        if r.gas:
            out[(r.chain, r.wallet, "native")].append((r.ts, -r.gas))
    return out


def holdings(rows: list[Row], live: dict[str, dict[str, str]]) -> list[Holding]:
    meta: dict[tuple[str, str, str], tuple[str, str]] = {}
    last_price: dict[tuple[str, str], Decimal] = {}
    for r in rows:
        meta[(r.chain, r.wallet, r.asset)] = (r.where, r.token)
        if r.price is not None and not r.sub.startswith("spam"):
            last_price[(r.chain, r.asset)] = r.price
    for r in rows:
        if r.gas:
            meta.setdefault((r.chain, r.wallet, "native"), (r.where, "(native)"))
    out = []
    for k, legs in _ledgers(rows).items():
        chain, wallet, asset = k
        qty = sum((d for _, d in legs), Decimal(0))
        lw = live.get(f"{chain}|{wallet}")
        lv = None if lw is None else _dec(lw.get(asset, "0"))
        if qty == 0 and (lv is None or lv == 0):
            continue
        where, sym = meta[k]
        out.append(Holding(chain, wallet, where, asset, sym, qty, last_price.get((chain, asset)), lv))
    return sorted(out, key=lambda h: (h.chain, h.where, -(h.usd or 0)))


@dataclass(slots=True)
class Gap:
    chain: str
    where: str
    symbol: str
    low: Decimal
    date: str
    end: Decimal


def gaps(rows: list[Row], eps: Decimal = Decimal("1e-6")) -> list[Gap]:
    """Running balances that dip below zero (after applying everything in the same second)."""
    sym = {(r.chain, r.wallet, r.asset): (r.where, r.token) for r in rows}
    out = []
    for k, legs in _ledgers(rows).items():
        bal, low, when = Decimal(0), Decimal(0), 0
        for ts, g in groupby(sorted(legs, key=lambda x: x[0]), key=lambda x: x[0]):
            bal += sum((d for _, d in g), Decimal(0))
            if bal < low:
                low, when = bal, ts
        if low < -eps:
            where, s = sym.get(k, (k[1], k[2]))
            out.append(Gap(k[0], where, s, low, datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d"), bal))
    return sorted(out, key=lambda g: g.low)


@dataclass(slots=True)
class Flow:
    row: Row
    usd: Decimal | None
    pair: Row | None = None
    pair_kind: str = ""

    @property
    def outflow(self) -> bool:
        return self.row.amount < 0


def boundary(rows: list[Row], min_usd: Decimal) -> tuple[list[Flow], int]:
    """Unlinked flows across the edge of the wallet list, with candidate pairings. Returns (flows, n_dust)."""
    cand = [r for r in rows if r.amount and not r.sub.startswith("spam") and not r.linked
            and (r.cls == "TRANSFER" and r.sub.split(":")[0] in BOUNDARY_SUBTYPES
                 or r.cls == "OTHER" and r.sub in ("bridge_in", "bridge_out", "self_or_passthrough", "contract_call"))]
    grouped: dict[tuple[str, str, str, str, bool], list[Row]] = defaultdict(list)
    for r in cand:  # one tx can carry several UTXO legs of the same asset
        grouped[(r.chain, r.wallet, r.tx, r.asset, r.amount < 0)].append(r)
    flows, dust = [], 0
    for legs in grouped.values():
        r = legs[0]
        if len(legs) > 1:
            r = Row(r.chain, r.ts, r.tx, r.wallet, r.label, r.token, r.asset, sum((x.amount for x in legs), Decimal(0)),
                    None if any(x.usd is None for x in legs) else sum((x.usd or Decimal(0) for x in legs), Decimal(0)),
                    r.price, r.cls, r.sub, r.gas, r.counterparty, r.linked, r.reason)
        if r.usd is not None and abs(r.usd) < min_usd:
            dust += 1
            continue
        flows.append(Flow(r, r.usd))
    ins = [f for f in flows if not f.outflow]
    used: set[int] = set()
    for f in sorted((f for f in flows if f.outflow), key=lambda f: f.row.ts):
        o = f.row
        best: tuple[float, Flow, str] | None = None
        for g in ins:
            i = g.row
            if id(g) in used or i.wallet == o.wallet and i.chain == o.chain or i.ts < o.ts - 600:
                continue
            dt = (i.ts - o.ts) / DAY
            if family(i.token) == family(o.token):
                ratio = i.amount / -o.amount
                if Decimal("0.95") <= ratio <= Decimal("1.0001") and dt <= 730:
                    score = float(1 - ratio) * 10 + dt / 30
                    kind = "same asset, hop via an unlisted address"
                else:
                    continue
            elif f.usd and g.usd and dt <= 2:
                ratio = g.usd / -f.usd
                if not Decimal("0.85") <= ratio <= Decimal("1.05"):
                    continue
                score = float(abs(1 - ratio)) * 10 + dt * 4 + 1
                kind = ("different asset within 6h, likely an instant-swap service" if dt <= 0.25
                        else f"different asset {dt * 24:.0f}h later, similar USD value: possible swap service")
            else:
                continue
            if best is None or score < best[0]:
                best = (score, g, kind)
        if best:
            used.add(id(best[1]))
            f.pair, f.pair_kind = best[1].row, best[2]
            best[1].pair, best[1].pair_kind = o, best[2]
            continue
        # several deposits into one wallet within 3 days that add up to the withdrawal (exchange batching)
        by_w: dict[tuple[str, str], list[Flow]] = defaultdict(list)
        for g in ins:
            i = g.row
            if id(g) not in used and family(i.token) == family(o.token) and o.ts - 600 <= i.ts <= o.ts + 3 * DAY \
                    and (i.chain, i.wallet) != (o.chain, o.wallet):
                by_w[(i.chain, i.wallet)].append(g)
        for gs in by_w.values():
            tot = sum((g.row.amount for g in gs), Decimal(0))
            if len(gs) > 1 and Decimal("0.95") <= tot / -o.amount <= Decimal("1.0001"):
                kind = f"same asset, {len(gs)} deposits adding up to {_q(tot)}"
                f.pair, f.pair_kind = gs[0].row, kind
                for g in gs:
                    used.add(id(g))
                    g.pair, g.pair_kind = o, kind
                break
    return sorted(flows, key=lambda f: f.row.ts), dust


def _q(d: Decimal) -> str:
    return f"{d:,.6f}".rstrip("0").rstrip(".") if d else "0"


def _u(d: Decimal | None) -> str:
    return "" if d is None else f"${d:,.2f}"


def report(rows_all: list[Row], live_snapshot: dict[str, Any], md: Path, holdings_csv: Path,
           min_usd: Decimal = Decimal(1)) -> dict[str, int]:
    fill_labels(rows_all)
    rows = real_rows(rows_all)
    live = live_snapshot.get("wallets", {}) if live_snapshot else {}
    hs = holdings(rows, live)
    gs = gaps(rows)
    fl, dust = boundary(rows, min_usd)
    when = datetime.fromtimestamp(int(live_snapshot.get("ts", 0)), UTC).strftime("%Y-%m-%d %H:%M UTC") \
        if live_snapshot else "never (run `classify verify` first)"

    with open(holdings_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "wallet", "wallet_label", "asset_id", "symbol", "quantity", "live_quantity", "status",
                    "last_usd_price", "usd_at_last_price"])
        for h in hs:
            w.writerow([h.chain, h.wallet, h.where, h.asset, h.symbol, f"{h.qty:f}",
                        "" if h.live is None else f"{h.live:f}", h.status, "" if h.price is None else f"{h.price:f}",
                        "" if h.usd is None else f"{h.usd:.2f}"])

    L: list[str] = ["# Custody reconciliation", ""]
    mism = [h for h in hs if h.status == "MISMATCH"]
    priced_mism = [h for h in mism if h.price is not None or h.asset == "native"]
    nolive = [h for h in hs if h.status == "no live check" and h.qty != 0]
    L += [f"Live balance snapshot: {when}.", "",
          f"- {len(hs)} non-zero holdings across {len({(h.chain, h.wallet) for h in hs})} wallets/accounts; "
          f"{sum(1 for h in hs if h.status == 'match')} match the live balance, {len(mism)} mismatch "
          f"({len(priced_mism)} of them priced/native), {len(nolive)} have no live source (exchanges without an API).",
          f"- {len(gs)} running balance(s) dip below zero.",
          f"- {len(fl)} unlinked boundary flow(s) ≥ ${min_usd} or unpriced ({dust} dust flows below that not listed); "
          f"{sum(1 for x in fl if x.pair and x.outflow)} outflow(s) have a candidate pairing.", ""]

    L += ["## Holdings by asset (all locations)", "", "| asset | quantity | locations | USD at last seen price |",
          "|---|---:|---|---:|"]
    by: dict[str, list[Holding]] = defaultdict(list)
    for h in hs:
        if h.qty > 0:
            by[family(h.symbol) if h.asset == "native" or h.price is not None else f"{h.symbol} ({h.asset[:10]})"].append(h)
    tot = Decimal(0)
    for fam, lst in sorted(by.items(), key=lambda kv: -sum((h.usd or 0 for h in kv[1]), Decimal(0))):
        usd = sum((h.usd for h in lst if h.usd is not None), Decimal(0))
        if not any(h.price is not None or h.asset == "native" for h in lst):
            continue
        tot += usd
        locs = ", ".join(f"{h.where} {_q(h.qty)}" for h in sorted(lst, key=lambda h: -h.qty)[:6])
        L.append(f"| {lst[0].symbol if len({h.symbol for h in lst}) == 1 else fam.upper()} | "
                 f"{_q(sum((h.qty for h in lst), Decimal(0)))} | {locs}{' …' if len(lst) > 6 else ''} | {_u(usd)} |")
    L += [f"| **total** | | | **{_u(tot)}** |", "",
          "Unpriced tokens (airdrops, NFTs, memecoins without a price feed) are left out of this table; they are in "
          f"`{holdings_csv.name}`.", ""]

    L += ["## Balance mismatches vs live", ""]
    if priced_mism:
        L += ["| chain | wallet | asset | ours | live | diff |", "|---|---|---|---:|---:|---:|"]
        L += [f"| {h.chain} | {h.where} | {h.symbol} | {_q(h.qty)} | {_q(h.live or Decimal(0))} | "
              f"{_q(h.qty - (h.live or Decimal(0)))} |" for h in priced_mism]
    else:
        L.append("None for any priced or native asset: every wallet with a live source matches exactly.")
    junk = [h for h in mism if h not in priced_mism]
    if junk:
        L += ["", f"{len(junk)} unpriced token(s) differ: "
              + ", ".join(f"{h.symbol} in {h.where} (ours {_q(h.qty)}, live {_q(h.live or Decimal(0))})" for h in junk)
              + ". These are unsolicited airdrops whose contracts emit transfer events without a real balance; "
                "they carry no value or cost basis."]
    L += ["", "## Running balances below zero", ""]
    if gs:
        L += ["| chain | wallet/account | asset | lowest | on | ending |", "|---|---|---|---:|---|---:|"]
        L += [f"| {g.chain} | {g.where} | {g.symbol} | {_q(g.low)} | {g.date} | {_q(g.end)} |" for g in gs]
    else:
        L.append("None.")
    L += ["", "## Boundary flows (value entering or leaving the listed wallets)", "",
          "Each needs a story: a purchase/income source for inflows, a sale/gift/payment or an unlisted own wallet "
          "for outflows. Pairings are suggestions only.", "",
          "| date (UTC) | chain | wallet/account | type | asset | amount | USD | pairing |", "|---|---|---|---|---|---:|---:|---|"]
    for x in fl:
        r = x.row
        pair = ""
        if x.pair:
            p = x.pair
            pair = f"{'→' if x.outflow else '←'} {p.date[:10]} {p.chain} {p.where} {_q(p.amount)} {p.token} ({x.pair_kind})"
        L.append(f"| {r.date} | {r.chain} | {r.where} | {r.sub} | {r.token} | {_q(r.amount)} | {_u(x.usd)} | {pair} |")
    unl = (live_snapshot or {}).get("unlisted_evm") or {}
    if unl:
        L += ["", "## Your EVM addresses on networks they are not listed for", "",
              "An EVM address exists on every EVM network; bridges credit the same address on the destination. "
              "Activity here means assets passed through (or still sit in) a wallet the list does not cover.", "",
              "| network | address | label elsewhere | native balance | txs sent |", "|---|---|---|---:|---:|"]
        L += [f"| {v['chain']} | {v['address']} | {v.get('label', '')} | {v['native']} | {v['nonce']} |"
              for v in unl.values()]
    md.write_text("\n".join(L) + "\n")
    return {"holdings": len(hs), "mismatch": len(mism), "priced_mismatch": len(priced_mism), "gaps": len(gs),
            "boundary_flows": len(fl), "paired": sum(1 for x in fl if x.pair and x.outflow)}
