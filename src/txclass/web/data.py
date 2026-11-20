"""Data behind the local web app: one cost-basis run (re-run on 'Refresh'), live prices, per-year tax views."""

from __future__ import annotations

import bisect
import csv
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import load_settings
from ..costbasis import Engine, Result, load as load_legs
from ..http import HttpClient
from ..reconcile import family
from ..taxforms import EXPORTS, Profile, TaxYear, build_year, year_notes

D0 = Decimal(0)
PARKED = ("escrow:", "staked:", "deposit:", "xp-chain:", "vault:", "offlist:")
INCOME_CATEGORY = {
    "airdrop": "Airdrop", "unsolicited_dust": "Airdrop", "promotional_gift": "Airdrop",
    "yield_interest": "Interest",
    "farming_reward": "Mining", "pool_reward": "Mining",
    "staking_reward": "Staking", "beacon_reward": "Staking", "proposer_reward": "Staking",
    "staking_reward_withdrawal": "Staking", "protocol_reward": "Staking", "governance_reward": "Staking",
    "validator_reward": "Staking", "beacon_withdrawal": "Staking",
    "claim": "Income", "exchange_credit": "Income", "deposit_refund_excess": "Income",
}
CATEGORIES = ["Airdrop", "Interest", "Mining", "Staking", "Income", "Other"]


def account_label(acct: str, labels: dict[str, str]) -> str:
    """Readable name for an engine account key (wallet, exchange, or a parked bucket)."""
    if acct.startswith("escrow:"):
        return f"Open order ({acct.split(':', 2)[1]} {acct.rsplit(':', 1)[-1][:10]}…)"
    if acct.startswith("staked:"):
        return f"Staked ({acct.split(':')[1]})"
    if acct.startswith("offlist:"):
        return {"dydx-chain": "dYdX chain (staked)"}.get(acct.split(":", 1)[1], acct.split(":", 1)[1])
    if acct.startswith(("deposit:", "xp-chain:", "vault:")):
        kind = {"deposit": "Stake deposit", "xp-chain": "Avalanche X/P-chain", "vault": "Yield vault"}[acct.split(":")[0]]
        return f"{kind} ({acct.split(':')[1]})"
    if "|" not in acct:
        return acct
    chain, addr = acct.split("|", 1)
    lab = labels.get(f"{chain}|{addr}") or labels.get(addr)
    return f"{lab} ({chain})" if lab else f"{chain} {addr[:6]}…{addr[-4:]}"


@dataclass
class Prices:
    usd: dict[str, Decimal]
    change24: dict[str, float]
    spark: dict[str, list[float]]
    fetched: float


class AppData:
    def __init__(self, root: Path, pre: str = "hifo", post: str = "hifo") -> None:
        self.root = root
        self.pre, self.post = pre, post
        self.lock = threading.Lock()
        self.settings = load_settings(None)
        self._prices: Prices | None = None
        self.refresh()

    # -- loading -------------------------------------------------------------------------------
    def refresh(self) -> None:
        with self.lock:
            classified = self.root / "classified.csv"
            self.res: Result = Engine(pre=self.pre, post=self.post).run(
                load_legs(classified, self.root / "manual.yaml"))
            self.profile = Profile.load(self.root / "tax_profile.yaml")
            self.loaded = datetime.now(UTC)
            self._years: dict[int, TaxYear] = {}
            self._read_classified(classified)
            self._labels = self._read_labels()

    def _read_labels(self) -> dict[str, str]:
        out: dict[str, str] = {}
        p = self.root / "addresses.csv"
        if p.exists():
            for r in csv.DictReader(open(p, newline="")):
                a, c, lab = (r.get("Address") or "").strip(), (r.get("Chain") or "").strip(), (r.get("Label") or "").strip()
                if a and lab:
                    out[f"{c}|{a.lower() if a.startswith('0x') else a}"] = lab
                    out.setdefault(a.lower() if a.startswith("0x") else a, lab)
        with open(self.root / "classified.csv", newline="") as f:
            for r in csv.DictReader(f):
                if r["wallet_label"]:
                    out.setdefault(f"{r['chain']}|{r['wallet'].split('#')[0]}", r["wallet_label"].replace(" · rewards", ""))
        return out

    def _read_classified(self, path: Path) -> None:
        """Price timeline per asset family, and a price-feed id per family for live prices."""
        from ..decode.exchanges import exchange_price_ids

        ex_ids = exchange_price_ids()
        timeline: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
        ids: dict[str, tuple[int, str]] = {}  # fam -> (priority, id)
        symbol: dict[str, str] = {}
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if not r["usd_price"] or r["subtype"].startswith("spam"):
                    continue
                fam = family(r["token"])
                timeline[fam].append((int(r["timestamp"]), Decimal(r["usd_price"])))
                symbol.setdefault(fam, fam.upper())
                chain = self.settings.chains.get(r["chain"])
                cand: tuple[int, str] | None = None
                if r["token"].upper() in ex_ids:
                    cand = (0, ex_ids[r["token"].upper()])
                elif chain is not None and r["token_address"] == "native" and chain.native_price_id:
                    cand = (1, chain.native_price_id)
                elif chain is not None and chain.price_chain and r["token_address"] not in ("", "native"):
                    cand = (2, f"{chain.price_chain}:{r['token_address']}")
                if cand and (fam not in ids or cand[0] < ids[fam][0]):
                    ids[fam] = cand
        manual = self.root / "manual.yaml"  # user-confirmed events may name a price feed (e.g. a staked airdrop)
        if manual.exists():
            import yaml

            for e in yaml.safe_load(manual.read_text()) or []:
                fam = family(str(e.get("token", "")))
                if e.get("price_id"):
                    ids[fam] = (-1, str(e["price_id"]))
                    symbol.setdefault(fam, fam.upper())
                if e.get("usd") and e.get("amount"):
                    ts = int(datetime.fromisoformat(str(e["date"])).replace(tzinfo=UTC).timestamp())
                    timeline[fam].append((ts, abs(Decimal(str(e["usd"])) / Decimal(str(e["amount"])))))
        for v in timeline.values():
            v.sort()
        self.timeline = dict(timeline)
        self.price_ids = {f: v[1] for f, v in ids.items()}
        self.symbol = symbol

    def price_at(self, fam: str, ts: int) -> Decimal | None:
        tl = self.timeline.get(fam)
        if not tl:
            return None
        i = bisect.bisect_right(tl, (ts, Decimal("Infinity"))) - 1
        return tl[i][1] if i >= 0 else None

    # -- live prices (DefiLlama, 5 minute cache) ------------------------------------------------------
    def prices(self, fams: list[str]) -> Prices:
        if self._prices and time.time() - self._prices.fetched < 300 and all(
                f in self._prices.usd or f not in self.price_ids for f in fams):
            return self._prices
        ids = {f: self.price_ids[f] for f in fams if f in self.price_ids}
        usd: dict[str, Decimal] = {}
        chg: dict[str, float] = {}
        spark: dict[str, list[float]] = {}
        http = HttpClient(3)
        rev = defaultdict(list)
        for f, i in ids.items():
            rev[i].append(f)
        keys = sorted(rev)
        try:
            for k in range(0, len(keys), 60):
                chunk = ",".join(keys[k:k + 60])
                cur = (http.request_json("GET", f"https://coins.llama.fi/prices/current/{chunk}") or {}).get("coins", {})
                for cid, v in cur.items():
                    for f in rev.get(cid, []):
                        usd[f] = Decimal(str(v["price"]))
                pct = (http.request_json("GET", f"https://coins.llama.fi/percentage/{chunk}",
                                         params={"period": "24h", "lookForward": "false"}) or {}).get("coins", {})
                for cid, v in pct.items():
                    for f in rev.get(cid, []):
                        chg[f] = float(v)
                ch = (http.request_json("GET", f"https://coins.llama.fi/chart/{chunk}",
                                        params={"span": 8, "period": "1d"}) or {}).get("coins", {})
                for cid, v in ch.items():
                    for f in rev.get(cid, []):
                        spark[f] = [float(p["price"]) for p in v.get("prices", [])]
        except Exception:  # offline: fall back to the last price seen in the history
            pass
        for f in fams:
            if f not in usd:
                p = self.price_at(f, int(time.time()))
                if p is not None:
                    usd[f] = p
        self._prices = Prices(usd, chg, spark, time.time())
        return self._prices

    # -- portfolio --------------------------------------------------------------------------------
    def accounts(self) -> list[tuple[str, str]]:
        accts = sorted({a for (a, f), lots in self.res.holdings.items() if lots and "@" not in f})
        return [(a, account_label(a, self._labels)) for a in accts]

    def assets(self, account: str = "") -> dict[str, Any]:
        agg: dict[str, list[Decimal]] = defaultdict(lambda: [D0, D0])
        per_acct: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(lambda: [D0, D0]))
        for (acct, fam), lots in self.res.holdings.items():
            if "@" in fam or (account and acct != account):
                continue
            for lot in lots:
                a = agg[fam]
                a[0] += lot.qty
                a[1] += lot.cost
                b = per_acct[fam][acct]
                b[0] += lot.qty
                b[1] += lot.cost
        fams = [f for f, (q, _) in agg.items() if q > 0]
        px = self.prices(fams)
        rows: list[dict[str, Any]] = []
        for f in fams:
            q, cost = agg[f]
            p = px.usd.get(f)
            mv = None if p is None else q * p
            holders = []
            for acct, (aq, ac) in sorted(per_acct[f].items(), key=lambda kv: -kv[1][0]):
                if aq <= 0:
                    continue
                amv = None if p is None else aq * p
                holders.append({"account": acct, "label": account_label(acct, self._labels), "qty": aq, "cost": ac,
                                "mv": amv, "unrealized": None if amv is None else amv - ac,
                                "share": float(aq / q * 100) if q else 0.0})
            rows.append({"fam": f, "symbol": self.symbol.get(f, f.upper()), "qty": q, "cost": cost,
                         "cost_per_unit": cost / q if q else D0, "price": p, "mv": mv,
                         "unrealized": None if mv is None else mv - cost, "chg24": px.change24.get(f),
                         "spark": px.spark.get(f, []), "holders": holders})
        total_mv = sum((r["mv"] for r in rows if r["mv"] is not None), D0)
        for r in rows:
            r["alloc"] = float(r["mv"] / total_mv * 100) if r["mv"] and total_mv else 0.0
        rows.sort(key=lambda r: -(r["mv"] or 0))
        return {"rows": rows, "mv": total_mv, "cost": sum((r["cost"] for r in rows), D0),
                "priced_at": datetime.fromtimestamp(px.fetched, UTC)}

    def history(self) -> list[dict[str, Any]]:
        out = []
        for month in sorted(self.res.monthly):
            y, m = map(int, month.split("-"))
            end = int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC).timestamp()) - 1
            cost = mv = D0
            for fam, (q, c) in self.res.monthly[month].items():
                cost += c
                p = self.price_at(fam, end)
                if p is not None:
                    mv += q * p
            out.append({"t": month, "cost": float(cost), "mv": float(mv)})
        return out

    # -- tax years --------------------------------------------------------------------------------
    def years(self) -> list[int]:
        ys = {datetime.fromtimestamp(d.ts, UTC).year for d in self.res.disposals}
        ys |= {datetime.fromtimestamp(x.ts, UTC).year for x in self.res.income}
        return sorted(ys, reverse=True)

    def tax_year(self, year: int) -> TaxYear:
        if year not in self._years:
            self._years[year] = build_year(self.res, year, self.profile, self.root / "classified.csv")
        return self._years[year]

    def year_summary(self, year: int) -> dict[str, Any]:
        ty = self.tax_year(year)
        st = sum((ln.gain for ln in ty.lines if ln.term == "short"), D0)
        lt = sum((ln.gain for ln in ty.lines if ln.term == "long"), D0)
        cats = {c: D0 for c in CATEGORIES}
        for typ, usd in ty.income.items():
            cats[INCOME_CATEGORY.get(typ, "Other")] += usd
        gifts_in = sum((x.usd or D0 for x in self.res.flags if x.what.startswith("gift received")
                        and datetime.fromtimestamp(x.ts, UTC).year == year), D0)
        gifts_out = sum((d.proceeds for d in self.res.gifts if datetime.fromtimestamp(d.ts, UTC).year == year), D0)
        # cumulative realized gains through the year (daily), for the chart
        daily: dict[str, Decimal] = defaultdict(Decimal)
        for ln in ty.lines:
            daily[datetime.fromtimestamp(ln.sold_ts, UTC).strftime("%Y-%m-%d")] += ln.gain
        run = D0
        series = []
        for day in sorted(daily):
            run += daily[day]
            series.append({"t": day, "v": float(run)})
        flags = [f for f in self.res.flags if datetime.fromtimestamp(f.ts, UTC).year == year]
        return {"year": year, "st": st, "lt": lt, "total": st + lt, "income": ty.income_total, "cats": cats,
                "gifts_in": gifts_in, "gifts_out": gifts_out, "series": series, "lines": len(ty.lines),
                "sched_d": ty.sched_d, "carry_in": ty.carry_in, "flags": flags, "notes": year_notes(ty),
                "boxes": ty.box_totals(), "eoy_cost": sum((lot.cost for lots in ty.positions.values() for lot in lots), D0)}

    def export(self, year: int, key: str) -> tuple[str, bytes]:
        e = next(x for x in EXPORTS if x.key == key)
        return e.filename.format(year=year), e.make(self.tax_year(year))
