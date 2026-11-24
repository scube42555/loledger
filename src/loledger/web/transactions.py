"""Transactions page: every classified.csv / manual.yaml row grouped into one transaction per (chain, tx, account),
with a plain-language type and server-side filters."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config import display_address
from ..costbasis import account
from .data import INCOME_CATEGORY

D0 = Decimal(0)
PAGE = 100

# Type shown on the page, in the order of the filter dropdown.
KINDS = ["Trade", "Order placed", "Order filled", "Order cancelled", "Income", "Own transfer", "Transfer",
         "Purchase", "Card top-up", "Fiat deposit", "Fiat withdrawal", "Bridge", "Wrap", "Liquidity", "Staking", "Futures", "Fee", "Gift",
         "Other", "Needs sync", "Failed", "Spam"]
# Which leg decides a transaction's type when its legs disagree (e.g. a swap with a fee leg).
_PRIORITY = ["FAILED", "SWAP", "ORDER_FILLED", "ORDER_PLACED", "ORDER_CANCELLED", "LP_DEPOSIT", "LP_WITHDRAW",
             "INTERNAL_TRANSFER", "TRANSFER", "OTHER"]
_FEES = {"exchange_withdrawal_fee", "account_maintenance", "network_fee", "fee"}
_STAKING = {"stake", "stake_management", "unstake_or_reward", "unstake", "delegate", "undelegate"}


def kind_of(cls: str, sub: str) -> str:
    """Plain-language type for a classification + subtype."""
    if cls == "FAILED":
        return "Failed"
    if sub == "awaiting_sync":
        return "Needs sync"
    if sub.startswith("spam"):
        return "Spam"
    if cls == "SWAP":
        return "Trade"
    if cls.startswith("ORDER_"):
        return {"ORDER_PLACED": "Order placed", "ORDER_FILLED": "Order filled"}.get(cls, "Order cancelled")
    if cls.startswith("LP_"):
        return "Liquidity"
    # what the subtype says beats the generic transfer classifications below
    if sub.startswith("exchanger_swap"):  # a cross-chain swap through a swap service: send here, receive there
        return "Trade"
    if sub.startswith("bridge"):
        return "Bridge"
    if cls == "INTERNAL_TRANSFER":
        return "Own transfer"
    if sub in ("fiat_deposit", "fiat_withdrawal"):
        return "Fiat deposit" if sub == "fiat_deposit" else "Fiat withdrawal"
    if cls == "TRANSFER":
        return "Transfer"
    if sub in ("payment", "purchased_item"):  # paying for something, and the item it bought
        return "Purchase"
    if sub == "card_topup":
        return "Card top-up"
    if sub in INCOME_CATEGORY:
        return "Income"
    if sub.startswith("futures"):
        return "Futures"
    if sub in ("wrap", "unwrap"):
        return "Wrap"
    if sub.startswith("gift"):
        return "Gift"
    if sub in _FEES or sub.endswith("_fee"):
        return "Fee"
    if sub in _STAKING:
        return "Staking"
    return "Other"


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


@dataclass(slots=True)
class TxLeg:
    token: str
    amount: Decimal
    usd: Decimal | None
    role: str
    cls: str
    sub: str
    counterparty: str
    reason: str
    linked: str
    confidence: str


@dataclass(slots=True)
class Tx:
    chain: str
    ts: int
    tx: str
    acct: str
    label: str
    kind: str
    sub: str
    legs: list[TxLeg] = field(default_factory=list)
    gas_native: Decimal | None = None
    gas_usd: Decimal | None = None
    gas_symbol: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts, UTC)

    @property
    def value(self) -> Decimal:
        """USD size: the larger of everything in and everything out (fee legs only when nothing else is priced)."""
        def side(legs: list[TxLeg]) -> Decimal:
            ins = sum((abs(lg.usd) for lg in legs if lg.usd is not None and lg.amount > 0), D0)
            outs = sum((abs(lg.usd) for lg in legs if lg.usd is not None and lg.amount < 0), D0)
            return max(ins, outs)

        return side([lg for lg in self.legs if lg.role != "fee"]) or side(self.legs)

    @property
    def fee_usd(self) -> Decimal | None:
        fees = [abs(lg.usd) for lg in self.legs if lg.role == "fee" and lg.usd is not None]
        if self.gas_usd is not None:
            fees.append(self.gas_usd)
        return sum(fees, D0) if fees else None

    @property
    def direction(self) -> int:
        """0 = only sends, 1 = both or neither, 2 = only receives (fee legs aside): a transfer's send comes
        before its receipt, so this orders same-time rows chronologically."""
        legs = [lg for lg in self.legs if lg.role != "fee"]
        out, inn = any(lg.amount < 0 for lg in legs), any(lg.amount > 0 for lg in legs)
        return 0 if out and not inn else 2 if inn and not out else 1

    @property
    def tokens(self) -> set[str]:
        return {lg.token.upper() for lg in self.legs}

    @property
    def summary(self) -> list[TxLeg]:
        """Net amount per token (fees folded in), outgoing first, for the one-line view."""
        net: dict[str, TxLeg] = {}
        for lg in self.legs:
            if lg.role == "fee" and len(self.legs) > 1 and any(x.role != "fee" for x in self.legs):
                continue
            s = net.get(lg.token)
            if s is None:
                net[lg.token] = TxLeg(lg.token, lg.amount, lg.usd if lg.usd is None else abs(lg.usd), lg.role, lg.cls,
                                      lg.sub, "", "", "", "")
            else:
                s.amount += lg.amount
                s.usd = None if s.usd is None or lg.usd is None else s.usd + abs(lg.usd)
        return sorted((x for x in net.values() if x.amount), key=lambda x: (x.amount > 0, x.token))

    def haystack(self) -> str:
        return " ".join([self.tx, self.sub, self.label, *(f"{lg.counterparty} {lg.reason} {lg.linked}"
                                                          for lg in self.legs)]).lower()


def build(rows: Iterable[Mapping[str, str]], labels: Callable[[str], str], natives: Mapping[str, str]) -> list[Tx]:
    """Group rows into transactions, newest first."""
    groups: dict[tuple[str, str, str], Tx] = {}
    first_cls: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        chain, w = r["chain"], r["wallet"]
        acct = account(chain, w)
        key = (chain, r["tx_hash"], acct)
        t = groups.get(key)
        if t is None:
            t = groups[key] = Tx(chain, int(r["timestamp"]), r["tx_hash"], acct, labels(acct), "", "",
                                 gas_symbol=natives.get(chain, ""))
        amt = _dec(r["amount"])
        if amt:
            usd = _dec(r.get("usd_value_at_time", ""))
            t.legs.append(TxLeg(r["token"] or "?", amt, usd, r.get("leg_role", ""), r["classification"], r["subtype"],
                                r.get("counterparty", ""), r.get("reason", ""), r.get("linked_tx_hash", ""),
                                r.get("confidence", "")))
        first_cls[key].append((r["classification"], r["subtype"]))
        g, gu = _dec(r.get("gas_fee_native", "")), _dec(r.get("gas_fee_usd", ""))
        if g:
            t.gas_native = max(t.gas_native or D0, g)
        if gu:
            t.gas_usd = max(t.gas_usd or D0, gu)
    for key, t in groups.items():
        cs = first_cls[key]
        cls, sub = min(cs, key=lambda c: _PRIORITY.index(c[0]) if c[0] in _PRIORITY else len(_PRIORITY))
        if all(s.startswith("spam") for _, s in cs):
            sub = cs[0][1]
        t.kind, t.sub = kind_of(cls, sub), sub
    # newest first; at the same time the receipt above its send (it completed the transfer), oldest-first reverses
    return sorted(groups.values(), key=lambda t: (-t.ts, -t.direction, t.chain, t.tx))



@dataclass
class Filters:
    accounts: list[str] = field(default_factory=list)  # each list: any of these (empty = all)
    chains: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    q: str = ""
    spam: bool = False
    sort: str = "new"

    @classmethod
    def parse(cls, a: Any) -> Filters:
        """From query args: a werkzeug MultiDict (repeated keys = several values) or a plain dict."""
        def one(k: str) -> str:
            v = a.get(k, "")
            return (v[0] if v else "") if isinstance(v, list) else str(v or "")

        def many(k: str) -> list[str]:
            vs = a.getlist(k) if hasattr(a, "getlist") else (a.get(k) or [])
            vs = [vs] if isinstance(vs, str) else vs
            return list(dict.fromkeys(x.strip() for v in vs for x in (v.split(",") if k == "asset" else [v])
                                      if x.strip()))

        def d(k: str) -> date | None:
            try:
                return date.fromisoformat(one(k)) if one(k) else None
            except ValueError:
                return None

        def n(k: str) -> Decimal | None:
            v = _dec(one(k).replace(",", "").replace("$", "").strip())
            return v if v is not None and v.is_finite() else None

        sort = one("sort")
        return cls(accounts=many("account"), chains=many("chain"), assets=many("asset"), kinds=many("kind"),
                   start=d("start"), end=d("end"), min=n("min"), max=n("max"), q=one("q").strip(),
                   spam=one("spam") == "1", sort=sort if sort in ("new", "old", "value") else "new")

    def active(self) -> bool:
        return any((self.accounts, self.chains, self.assets, self.kinds, self.start, self.end,
                    self.min is not None, self.max is not None, self.q, self.spam))

    def apply(self, txs: list[Tx]) -> list[Tx]:
        assets, kinds, q = {x.upper() for x in self.assets}, set(self.kinds), self.q.lower()
        accounts, chains = set(self.accounts), set(self.chains)
        lo = int(datetime(self.start.year, self.start.month, self.start.day, tzinfo=UTC).timestamp()) \
            if self.start else None
        hi = int(datetime(self.end.year, self.end.month, self.end.day, tzinfo=UTC).timestamp()) + 86400 \
            if self.end else None
        out = []
        for t in txs:
            if kinds:
                if t.kind not in kinds and not (self.spam and t.kind == "Spam"):
                    continue
            elif t.kind == "Spam" and not self.spam:
                continue
            if accounts and t.acct not in accounts or chains and t.chain not in chains:
                continue
            if assets and not assets & t.tokens or lo is not None and t.ts < lo or hi is not None and t.ts >= hi:
                continue
            if self.min is not None and t.value < self.min or self.max is not None and t.value > self.max:
                continue
            if q and q not in t.haystack():
                continue
            out.append(t)
        if self.sort == "old":
            out.reverse()
        elif self.sort == "value":
            out.sort(key=lambda t: -t.value)
        return out


def to_csv(txs: Iterable[Tx]) -> bytes:
    """One row per leg of the filtered transactions."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date_utc", "chain", "account", "type", "subtype", "tx_hash", "token", "amount", "usd_value", "role",
                "classification", "counterparty", "gas_fee_native", "gas_fee_usd", "reason"])
    for t in txs:
        for i, lg in enumerate(t.legs or [TxLeg("", D0, None, "", "", t.sub, "", "", "", "")]):
            w.writerow([t.when.strftime("%Y-%m-%d %H:%M:%S"), t.chain, t.label, t.kind, lg.sub, t.tx, lg.token,
                        f"{lg.amount:f}", "" if lg.usd is None else f"{lg.usd:f}", lg.role, lg.cls, display_address(lg.counterparty),
                        "" if i or t.gas_native is None else f"{t.gas_native:f}",
                        "" if i or t.gas_usd is None else f"{t.gas_usd:f}", lg.reason])
    return buf.getvalue().encode()
