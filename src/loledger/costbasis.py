"""Cost basis engine over classified.csv: lots, gains, income, and the 2025 per-wallet transition.

Method (user-confirmed 2026-11-19; HIFO is what the 2022-2024 returns were filed with):
- Through 2024-12-31: universal HIFO, one pool per asset across all wallets (as filed for 2022-2024).
- 2025-01-01 (Rev. Proc. 2024-28 safe harbor): the universal pool's unused lots are allocated to the
  wallets that actually held the units, guided by a traced per-wallet ledger run in parallel
  (lots that travelled with transfers are matched by lot id first; the rest fill remaining capacity).
- From 2025-01-01: HIFO within each wallet / exchange account (a standing order kept in the user's records);
  transfers between own wallets carry basis and acquisition date, moving lots in the same HIFO order.
  Both methods are settings (Engine(pre=..., post=...), `loledger basis --method-pre/--method-post`).
- Network/exchange fees paid in crypto are small disposals at FMV; their value is added to the basis
  of what the same transaction acquired, or deducted from its sale proceeds.

Assets are pooled by family (WETH = ETH, MATIC = POL, USDC.e = USDC). Fiat USD carries no lots.
Spam legs only count when the asset is real (priced or native): such dust is booked as income at FMV.

Everything is computed from classified.csv; CoinLedger is never an input.
"""

from __future__ import annotations

import bisect
import csv
import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .reconcile import family

D0 = Decimal(0)
BOUNDARY = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
EXCHANGES = {"MEXC", "Crypto.com", "Gemini", "OKX", "Binance.US", "Coinbase"}
FIAT = {"usd"}
INCOME = {"staking_reward", "pool_reward", "beacon_reward", "proposer_reward", "farming_reward", "exchange_credit",
          "staking_reward_withdrawal", "protocol_reward", "governance_reward", "beacon_withdrawal",
          "validator_reward", "promotional_gift", "claim", "airdrop"}
MOVE_SUBTYPES = {"bridge_in", "bridge_out", "self_send", "self_or_passthrough", "wrap", "unwrap", "admin",
                 "from_exchange", "to_exchange", "withdrawal_to_chain", "deposit_from_chain", "from_eoa", "to_eoa",
                 "to_contract", "unknown", "pooled_wallet_coins"}
# escrow-like parking: (escrow key template, income type for any excess that comes back)
PARK_IN = {"bridge_to": "offlist:{dest}", "stake": "staked:{chain}:{fam}", "stake_registration": "deposit:{chain}:{fam}",
           "atomic_export": "xp-chain:{chain}:{fam}", "yield_deposit": "vault:{acct}:{fam}"}
PARK_OUT = {"unstake_or_reward": ("staked:{chain}:{fam}", "staking_reward"),
            "beacon_exit": ("staked:{chain}:{fam}", "staking_reward"),
            "stake_deregistration": ("deposit:{chain}:{fam}", "deposit_refund_excess"),
            "atomic_import": ("xp-chain:{chain}:{fam}", "staking_reward"),
            "yield_withdraw": ("vault:{acct}:{fam}", "yield_interest")}
NOISE = {"approval", "contract_call", "stake_management", "account_maintenance", "delegation", "governance_vote",
         "consensus_keyreg", "order_change", "staking_order_settled_noop"}


def _dec(s: str) -> Decimal:
    try:
        return Decimal(s) if s else D0
    except InvalidOperation:
        return D0


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _long_term(acq: int, sold: int) -> bool:
    a, s = datetime.fromtimestamp(acq, UTC), datetime.fromtimestamp(sold, UTC)
    try:
        anniv = a.replace(year=a.year + 1)
    except ValueError:  # Feb 29
        anniv = a.replace(year=a.year + 1, day=28)
    return s > anniv


@dataclass(slots=True)
class Leg:
    i: int
    chain: str
    ts: int
    tx: str
    wallet: str
    acct: str  # tax account: exchange name, or chain|address (Cardano reward account folded into its stake key)
    token: str
    fam: str
    amount: Decimal
    usd: Decimal | None  # absolute FMV of this leg
    cls: str
    sub: str
    role: str
    linked: str
    order: str
    kind: str = ""
    note: str = ""

    @property
    def st(self) -> str:
        return self.sub.split(":")[0]


def account(chain: str, wallet: str) -> str:
    if chain in EXCHANGES:
        return chain
    return f"{chain}|{wallet.split('#')[0]}"


def manual_rows(path: Path) -> list[dict[str, str]]:
    """manual.yaml entries as classified.csv-shaped rows (user-confirmed events on unfetchable chains)."""
    if not path.exists():
        return []
    out = []
    for i, e in enumerate(yaml.safe_load(path.read_text()) or []):
        ts = int(datetime.fromisoformat(str(e["date"])).replace(tzinfo=UTC).timestamp())
        amt = Decimal(str(e["amount"]))
        usd = Decimal(str(e.get("usd") or 0))
        out.append({"chain": str(e["chain"]), "timestamp": str(ts), "tx_hash": f"manual:{i}", "wallet": str(e["wallet"]),
                    "token": str(e["token"]), "token_address": "native", "amount": f"{amt:f}",
                    "usd_value_at_time": f"{usd:f}" if usd else "", "usd_price": f"{usd / abs(amt):f}" if usd else "",
                    "classification": str(e.get("classification") or "TRANSFER"), "subtype": str(e["subtype"]),
                    "leg_role": "", "linked_tx_hash": "", "order_id": "", "gas_fee_native": "", "gas_fee_usd": ""})
    return out


def load(path: Path, manual: Path | None = None) -> list[Leg]:
    rows = list(csv.DictReader(open(path, newline=""))) + (manual_rows(manual) if manual else [])
    priced = {(r["chain"], r["token_address"]) for r in rows if r["usd_price"] and not r["subtype"].startswith("spam")}
    legs: list[Leg] = []
    for r in rows:
        chain, w, asset = r["chain"], r["wallet"], r["token_address"] or r["token"]
        real = asset == "native" or (chain, asset) in priced
        fam = family(r["token"]) if real or chain in EXCHANGES else f"{r['token']}@{asset}"
        usd = abs(_dec(r["usd_value_at_time"])) if r["usd_value_at_time"] else None
        ts, tx, cls, sub = int(r["timestamp"]), r["tx_hash"], r["classification"], r["subtype"]
        acct, linked, order = account(chain, w), r["linked_tx_hash"], r["order_id"] or ""
        amt = _dec(r["amount"])
        if amt and (real or not sub.startswith("spam")):
            legs.append(Leg(len(legs), chain, ts, tx, w, acct, r["token"], fam, amt, usd, cls, sub, r["leg_role"],
                            linked, order))
        gas = _dec(r["gas_fee_native"])
        if gas:
            legs.append(Leg(len(legs), chain, ts, tx, w, acct, "(gas)", "", -gas,
                            abs(_dec(r["gas_fee_usd"])) if r["gas_fee_usd"] else None, cls, sub, "gas", linked, order))
    # gas legs: the chain's native family (taken from any native leg on that chain)
    native_fam: dict[str, str] = {}
    for r in rows:
        if r["token_address"] == "native":
            native_fam.setdefault(r["chain"], family(r["token"]))
    for lg in legs:
        if lg.role == "gas":
            lg.fam = native_fam.get(lg.chain, lg.chain.lower())
            lg.token = lg.fam.upper()
    legs.sort(key=lambda x: (x.ts, x.i))
    for lg in legs:
        lg.kind = kind(lg)
    _own_account_stakes(legs)
    _cross_asset_bridges(legs)
    return legs


def _cross_asset_bridges(legs: list[Leg]) -> None:
    """A bridge/transfer out whose linked tx brings back a *different* asset (and none of the same) was a
    cross-chain swap: a disposal at FMV, the other side an acquisition."""
    by_tx: dict[str, list[Leg]] = defaultdict(list)
    for lg in legs:
        by_tx[lg.tx].append(lg)
    for lg in legs:
        if lg.kind != "move_out" or not lg.linked:
            continue
        back = [x for x in by_tx.get(lg.linked, []) if x.amount > 0]
        if back and all(x.kind == "acquire" and x.st == "exchanger_swap_in" for x in back) \
                and not any(x.fam == lg.fam for x in back):
            lg.kind = "dispose"


def _own_account_stakes(legs: list[Leg]) -> None:
    """A stake/unstake whose other side is another listed account in the same tx (a wallet funding its
    own listed stake account) is a plain move, not a deposit into staking."""
    ins: dict[tuple[str, str, str, Decimal], list[Leg]] = defaultdict(list)
    for lg in legs:
        if lg.amount > 0 and lg.kind in ("park_out", "move_in", "acquire", "income"):
            ins[(lg.chain, lg.tx, lg.fam, lg.amount)].append(lg)
    for lg in legs:
        if lg.kind != "park_in":
            continue
        other = next((x for x in ins.get((lg.chain, lg.tx, lg.fam, -lg.amount), []) if x.acct != lg.acct), None)
        if other is not None:
            lg.kind, other.kind = "move_out", "move_in"


def kind(lg: Leg) -> str:
    st, cls, a = lg.st, lg.cls, lg.amount
    if lg.fam in FIAT:
        return "fiat"
    if st in ("futures_realized_pnl", "futures_fee"):
        return "futures"
    if lg.role == "gas" or lg.role == "fee" or st in ("exchange_withdrawal_fee", "account_rent"):
        return "fee"
    if st.startswith("spam"):
        return "income" if a > 0 else "fee"
    if st in ("bridge_receipt",) or cls == "FAILED":
        return "ignore"
    if st in ("futures_realized_pnl", "futures_fee"):
        return "futures"
    if st == "gift_sent":
        return "gift_out"
    if st in ("gift_received", "purchased_item", "exchanger_swap_in"):
        return "acquire"
    if st in ("payment", "card_topup", "exchanger_swap_out"):  # spending crypto: a disposal at FMV
        return "dispose"
    if st in INCOME:
        return "income" if st != "airdrop" or lg.usd else "acquire"
    if st in PARK_IN:
        return "park_in"
    if st in PARK_OUT:
        return "park_out"
    if cls == "ORDER_PLACED":
        return "escrow_in"
    if cls == "ORDER_CANCELLED":
        return "escrow_out"
    if cls in ("ORDER_FILLED", "LP_DEPOSIT", "LP_WITHDRAW") and lg.role in ("min_ada", "refund") and a > 0 \
            and (lg.order or lg.linked):
        return "escrow_out"
    if cls == "ORDER_FILLED":
        return "swap_in" if a > 0 else "swap_out"
    if cls in ("SWAP", "LP_DEPOSIT", "LP_WITHDRAW", "LP_FEES_CLAIMED") or st == "staking_order_settled":
        return "swap_in" if a > 0 else "swap_out"
    if cls == "INTERNAL_TRANSFER" or st in MOVE_SUBTYPES or cls == "TRANSFER":
        return "move_in" if a > 0 else "move_out"
    if st in NOISE:
        return "acquire" if a > 0 else "fee"
    return "acquire" if a > 0 else "dispose"


# ---------------------------------------------------------------------------------------------- lots


@dataclass(slots=True)
class Lot:
    lot_id: int
    qty: Decimal
    cost: Decimal  # total USD basis of qty
    acq: int
    origin: str  # what created it
    flag: str = ""
    gift_fmv: Decimal | None = None  # a gift with the donor's basis: FMV when received (the loss basis if lower)
    inherited: bool = False  # inherited property (or a gift of it): always long-term, "INHERITED" on Form 8949

    def split(self, q: Decimal) -> Lot:
        """Cut q off this lot (q < qty) and return it."""
        part = replace(self, qty=q, cost=self.cost * q / self.qty,
                       gift_fmv=None if self.gift_fmv is None else self.gift_fmv * q / self.qty)
        self.cost -= part.cost
        if self.gift_fmv is not None and part.gift_fmv is not None:
            self.gift_fmv -= part.gift_fmv
        self.qty -= q
        return part


class Pools:
    """FIFO lot pools. Taking more than is held creates a zero-basis 'missing_basis' lot and records it as a
    debt; the next units added to that pool repay the debt first, giving the earlier disposal (or move) the
    basis and acquisition date of those units. This absorbs ordering noise (a sale processed a moment before
    the deposit that funded it) without inflating holdings."""

    def __init__(self, method: str = "fifo") -> None:
        """method: fifo (oldest acquisition first) or hifo (highest unit cost first, then oldest).
        Each pool is kept sorted in that order, so takes always come off the front."""
        if method not in ("fifo", "hifo"):
            raise ValueError(f"unknown lot method {method!r}")
        self.method = method
        self.p: dict[tuple[str, str], list[Lot]] = defaultdict(list)
        self.debt: dict[tuple[str, str], deque[list[object]]] = defaultdict(deque)  # [lot, qty still unfunded]

    def add(self, key: tuple[str, str], lot: Lot) -> None:
        dq = self.debt.get(key)
        while dq and lot.qty > 0:
            entry = dq[0]
            d, need = entry[0], entry[1]
            assert isinstance(d, Lot) and isinstance(need, Decimal)
            m = min(need, lot.qty)
            cost = lot.cost * m / lot.qty
            d.cost += cost
            if d.flag == "missing_basis":
                d.acq, d.lot_id, d.origin = min(d.acq, lot.acq), lot.lot_id, f"late-funded by {lot.origin}"
            lot.cost -= cost
            lot.qty -= m
            entry[1] = need - m
            if need - m <= 0:
                d.flag = "funded_late" if d.flag == "missing_basis" else d.flag
                dq.popleft()
        if lot.qty > 0:
            bisect.insort(self.p[key], lot, key=self._order)

    def _order(self, lot: Lot) -> tuple[Decimal, int, int]:
        if self.method == "hifo":
            return (-(lot.cost / lot.qty) if lot.qty else D0, lot.acq, lot.lot_id)
        return (D0, lot.acq, lot.lot_id)

    def take(self, key: tuple[str, str], qty: Decimal, ts: int, why: str) -> list[Lot]:
        """Lots in the pool's order. A shortfall comes back as a zero-basis lot flagged missing_basis, acquired at ts."""
        out: list[Lot] = []
        q = qty
        dq = self.p.get(key)
        while q > 0 and dq:
            lot = dq[0]
            if lot.qty <= q:
                out.append(dq.pop(0))
                q -= lot.qty
            else:
                out.append(lot.split(q))
                q = D0
        if q > Decimal("1e-12"):
            miss = Lot(-1, q, D0, ts, why, "missing_basis")
            out.append(miss)
            self.debt[key].append([miss, q])
        return out

    def qty(self, key: tuple[str, str]) -> Decimal:
        return sum((x.qty for x in self.p.get(key, ())), D0)


@dataclass(slots=True)
class Disposal:
    ts: int
    acct: str
    fam: str
    lot: Lot
    proceeds: Decimal
    event: str
    tx: str
    method: str

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.lot.cost

    @property
    def term(self) -> str:
        return "long" if self.lot.inherited or _long_term(self.lot.acq, self.ts) else "short"


@dataclass(slots=True)
class Income:
    ts: int
    acct: str
    fam: str
    qty: Decimal
    usd: Decimal
    typ: str
    tx: str


@dataclass(slots=True)
class Flag:
    ts: int
    acct: str
    fam: str
    qty: Decimal
    usd: Decimal | None
    what: str
    tx: str


@dataclass
class Result:
    disposals: list[Disposal] = field(default_factory=list)
    income: list[Income] = field(default_factory=list)
    futures: list[Income] = field(default_factory=list)  # signed USD = realized contract P&L (fees negative)
    gifts: list[Disposal] = field(default_factory=list)  # proceeds = FMV at gift (not a sale)
    flags: list[Flag] = field(default_factory=list)
    allocation: list[tuple[str, str, Lot, str]] = field(default_factory=list)  # acct, fam, lot, how
    unallocated: list[tuple[str, Lot]] = field(default_factory=list)
    holdings: dict[tuple[str, str], list[Lot]] = field(default_factory=dict)
    year_end: dict[int, dict[tuple[str, str], list[Lot]]] = field(default_factory=dict)  # holdings at Dec 31
    monthly: dict[str, dict[str, tuple[Decimal, Decimal]]] = field(default_factory=dict)  # "YYYY-MM" -> fam -> (qty, cost)
    written_off: list[tuple[int, str, str, Decimal, Decimal]] = field(default_factory=list)  # closed accounts, no deduction
    methods: tuple[str, str] = ("hifo", "hifo")


# ------------------------------------------------------------------------------------------ engine


class Engine:
    """Runs the universal ledger (gains through 2024) and the traced per-wallet ledger in parallel,
    switches to the allocated per-wallet ledger at 2025-01-01."""

    def __init__(self, boundary: int = BOUNDARY, pre: str = "hifo", post: str = "hifo",
                 closures: dict[str, Any] | None = None, gifts: list[dict[str, Any]] | None = None) -> None:
        """pre: lot method of the universal pool through 2024 (as filed); post: per-wallet method from 2025.
        Before the boundary the per-wallet ledger only traces which units physically moved (oldest first).
        closures: account -> date the exchange closed it, or {date, treatment} (tax_profile.yaml closed_accounts).
        What the books still hold there that day leaves them: by default written off with no deduction claimed
        (no sale or exchange happened); with treatment: capital_loss, disposed of for $0 instead (on Form 8949)."""
        self.boundary = boundary
        self.closures: dict[str, tuple[int, bool]] = {}  # account -> (closing time, claim a capital loss)
        for a, v in (closures or {}).items():
            date, treatment = (v.get("date"), v.get("treatment", "")) if isinstance(v, dict) else (v, "")
            self.closures[a] = (int(datetime.fromisoformat(str(date)).replace(tzinfo=UTC).timestamp()),
                                str(treatment).replace("-", "_") == "capital_loss")
        # gifts with a known history (tax_profile.yaml gifts_received): tx -> (donor basis per unit, the date the
        # holding period starts, inherited). Inherited: basis = FMV at the date of death, always long-term.
        self.gifts: dict[str, tuple[Decimal, int, bool]] = {}
        for g in gifts or []:
            day = g.get("inherited") or g.get("acquired")
            start = int(datetime.fromisoformat(str(day)).replace(tzinfo=UTC).timestamp())
            for tx in g.get("txs") or []:
                self.gifts[str(tx)] = (Decimal(str(g["basis_per_unit"])), start, bool(g.get("inherited")))
        self.pre, self.post = pre, post
        self.universal = Pools(pre)  # key ("*", fam)
        self.wallets = Pools("fifo")  # traced before the boundary, authoritative after
        self.switched = False
        self.ids = itertools.count(1)
        self.res = Result(methods=(pre, post))

    # -- primitives --------------------------------------------------------------------------
    def _maybe_switch(self, ts: int) -> None:
        if not self.switched and ts >= self.boundary:
            self._allocate()
            self.switched = True

    def acquire(self, ts: int, acct: str, fam: str, qty: Decimal, cost: Decimal, origin: str, flag: str = "",
                gift_fmv: Decimal | None = None, inherited: bool = False) -> None:
        lot = Lot(next(self.ids), qty, cost, ts, origin, flag, gift_fmv, inherited)
        self.wallets.add((acct, fam), lot)
        if not self.switched:
            self.universal.add(("*", fam), replace(lot))

    def dispose(self, ts: int, acct: str, fam: str, qty: Decimal, proceeds: Decimal, event: str, tx: str,
                gift: bool = False) -> None:
        if qty <= 0:
            return
        traced = self.wallets.take((acct, fam), qty, ts, event)
        if self.switched:
            lots, method = traced, f"wallet_{self.post}"
        else:
            lots, method = self.universal.take(("*", fam), qty, ts, event), f"universal_{self.pre}"
        for lot in lots:
            share = proceeds * lot.qty / qty
            if lot.gift_fmv is not None and lot.gift_fmv < lot.cost and not gift:
                # gift worth less than the donor's basis when received (IRC 1015(a)): the donor's basis for a gain,
                # the FMV at the gift for a loss, and no gain or loss for a sale in between
                lot.cost = lot.cost if share >= lot.cost else lot.gift_fmv if share <= lot.gift_fmv else share
            d = Disposal(ts, acct, fam, lot, share, event, tx, method)
            (self.res.gifts if gift else self.res.disposals).append(d)

    def _close(self, ts: int, acct: str) -> None:
        """The exchange closed the account: what is still held there leaves the books."""
        loss = self.closures[acct][1]
        for (a, fam), lots in list(self.wallets.p.items()):
            qty = sum((lot.qty for lot in lots if lot.qty > 0), D0)
            if a != acct or qty <= 0:
                continue
            cost = sum((lot.cost for lot in lots if lot.qty > 0), D0)
            if loss:
                self.dispose(ts, acct, fam, qty, D0, "account_closed", f"closed:{acct}")
                what = (f"left on {acct} when the exchange closed the account: disposed of for $0 (a capital loss of "
                        f"the ${cost:,.2f} basis, per tax_profile.yaml); confirm with your tax preparer")
            else:
                self.wallets.take((acct, fam), qty, ts, "account_closed")
                if not self.switched:
                    self.universal.take(("*", fam), qty, ts, "account_closed")
                self.res.written_off.append((ts, acct, fam, qty, cost))
                what = (f"left on {acct} when the exchange closed the account: written off with no deduction claimed "
                        f"(${cost:,.2f} basis); set treatment: capital_loss in tax_profile.yaml if your preparer "
                        "approves a loss")
            self.res.flags.append(Flag(ts, acct, fam, qty, cost, what, f"closed:{acct}"))

    def move(self, ts: int, src: str, dst: str, fam: str, qty: Decimal, why: str) -> None:
        if qty <= 0 or src == dst:
            return
        for lot in self.wallets.take((src, fam), qty, ts, why):
            self.wallets.add((dst, fam), lot)

    # -- 2025-01-01 allocation ------------------------------------------------------------------
    def _allocate(self) -> None:
        fams = {k[1] for k in self.universal.p} | {k[1] for k in self.wallets.p}
        new = Pools(self.post)
        for fam in sorted(fams):
            pool = [lot for lot in self.universal.p.get(("*", fam), ()) if lot.qty > 0]
            by_id: dict[int, Lot] = {lot.lot_id: lot for lot in pool}
            holders = {k[0]: list(dq) for k, dq in self.wallets.p.items() if k[1] == fam and dq}
            cap = {a: sum((x.qty for x in lots), D0) for a, lots in holders.items()}
            got: dict[str, list[Lot]] = defaultdict(list)
            for a, lots in holders.items():  # 1. traced: the same lot is still in this wallet
                for t in lots:
                    u = by_id.get(t.lot_id)
                    if u is None or u.qty <= 0 or cap[a] <= 0:
                        continue
                    q = min(u.qty, t.qty, cap[a])
                    part = u.split(q) if q < u.qty else replace(u)
                    if q >= u.qty:
                        u.qty, u.cost = D0, D0
                    got[a].append(part)
                    self.res.allocation.append((a, fam, part, "traced"))
                    cap[a] -= q
            rest = sorted((u for u in pool if u.qty > 0), key=lambda x: x.acq)
            for u in rest:  # 2. remaining unused basis fills remaining capacity, largest holder first
                for a in sorted(cap, key=lambda a: -cap[a]):
                    if u.qty <= 0:
                        break
                    if cap[a] <= 0:
                        continue
                    q = min(u.qty, cap[a])
                    part = u.split(q) if q < u.qty else replace(u)
                    if q >= u.qty:
                        u.qty, u.cost = D0, D0
                    got[a].append(part)
                    self.res.allocation.append((a, fam, part, "assigned"))
                    cap[a] -= q
                if u.qty > 0:
                    self.res.unallocated.append((fam, replace(u)))
            for a, c in cap.items():  # 3. units held with no unused basis left
                if c > Decimal("1e-12"):
                    acq = min((t.acq for t in holders[a]), default=self.boundary)
                    lot = Lot(next(self.ids), c, D0, acq, "2025-01-01 allocation", "no_basis_at_allocation")
                    got[a].append(lot)
                    self.res.allocation.append((a, fam, lot, "no basis left"))
            for a, lots in got.items():
                for lot in sorted(lots, key=lambda x: x.acq):
                    new.add((a, fam), lot)
        self.wallets = new

    # -- driver ---------------------------------------------------------------------------------
    def run(self, legs: list[Leg]) -> Result:
        pairs = pair_moves(legs)
        swap_services(legs, {id(x) for p in pairs for x in p})
        orders = OrderBook(legs)
        groups: dict[tuple[str, str, str], list[Leg]] = defaultdict(list)
        for lg in legs:
            groups[(lg.chain, lg.tx, lg.acct)].append(lg)
        # schedule: each group at its first leg; each move pair at min(out, in)
        # within one second (one block), anything that only brings assets in goes first: a spend can be
        # funded by another tx in the same block that the file lists after it
        sched: list[tuple[int, int, int, str, object]] = []
        for g in groups.values():
            settles = bool(orders.keys_for(g)) or any(lg.cls == "ORDER_CANCELLED" or lg.kind == "escrow_out" for lg in g)
            prio = 2 if settles else 0 if all(lg.amount > 0 for lg in g) else 1
            sched.append((g[0].ts, prio, g[0].i, "group", g))
        for o, i in pairs:
            sched.append((min(o.ts, i.ts), 1, min(o.i, i.i), "pair", (o, i)))
        for acct, (ts, _loss) in self.closures.items():  # after everything else at that second
            sched.append((ts, 3, 0, "close", acct))
        sched.sort(key=lambda x: (x[0], x[1], x[2]))
        paired = {id(x) for p in pairs for x in p}
        transit_by_fam: dict[str, deque[Lot]] = defaultdict(deque)
        year = None
        month = ""
        for ts, _p, _i, what, obj in sched:
            dt = datetime.fromtimestamp(ts, UTC)
            y, mo = dt.year, dt.strftime("%Y-%m")
            self._maybe_switch(ts)  # first, so the 2024 year-end shows the basis allocated at 2025-01-01
            if year is not None and y != year:
                self._snapshot(year)
            if month and mo != month:
                self._month(month)
            year, month = y, mo
            if what == "close":
                self._close(ts, str(obj))
            elif what == "pair":
                o, i = obj  # type: ignore[misc]
                self._pair(o, i)
            else:
                self._group(obj, paired, orders, transit_by_fam)  # type: ignore[arg-type]
        self._maybe_switch(self.boundary)
        for fam, dq in transit_by_fam.items():
            for lot in dq:
                if lot.qty > 0:
                    self.res.flags.append(Flag(lot.acq, "transit", fam, lot.qty, lot.cost,
                                               "left custody to an unknown address and never came back: "
                                               "basis carried out of the books (no gain/loss booked)", lot.origin))
        self.res.holdings = {k: list(v) for k, v in self.wallets.p.items() if v}
        if year is not None:
            self._snapshot(year)
            self._month(month)
        for d in self.res.disposals:
            if d.lot.flag == "missing_basis" and d.proceeds >= Decimal("0.01"):
                self.res.flags.append(Flag(d.ts, d.acct, d.fam, d.lot.qty, d.proceeds,
                                           f"sold more than held ({d.event}): zero basis used", d.tx))
        return self.res

    def _snapshot(self, year: int) -> None:
        """Holdings (copies of the lots) as of the end of `year`."""
        self.res.year_end[year] = {k: [replace(x) for x in v if x.qty > 0] for k, v in self.wallets.p.items() if v}

    def _month(self, month: str) -> None:
        """Quantity and cost basis per asset family (all accounts) at the end of `month`."""
        agg: dict[str, list[Decimal]] = defaultdict(lambda: [D0, D0])
        for (_acct, fam), lots in self.wallets.p.items():
            if "@" in fam:
                continue
            for lot in lots:
                a = agg[fam]
                a[0] += lot.qty
                a[1] += lot.cost
        self.res.monthly[month] = {f: (v[0], v[1]) for f, v in agg.items()}

    def _pair(self, o: Leg, i: Leg) -> None:
        q_out, q_in = -o.amount, i.amount
        moved = min(q_out, q_in)
        self.move(o.ts, o.acct, i.acct, o.fam, moved, f"transfer {o.tx[:16]}")
        if q_out > q_in:  # the gap was a fee taken in transit
            price = (o.usd / q_out) if o.usd and q_out else D0
            self.dispose(o.ts, o.acct, o.fam, q_out - q_in, (q_out - q_in) * price, "transfer_fee", o.tx)
        elif q_in > q_out:
            extra = q_in - q_out
            price = (i.usd / q_in) if i.usd and q_in else D0
            self.acquire(i.ts, i.acct, i.fam, extra, extra * price, f"transfer surplus {i.tx[:16]}", "transfer_surplus")

    def _group(self, g: list[Leg], paired: set[int], orders: OrderBook, transit: dict[str, deque[Lot]]) -> None:
        fees = [lg for lg in g if lg.kind == "fee"]
        swap_out = [lg for lg in g if lg.kind == "swap_out"]
        swap_in = [lg for lg in g if lg.kind == "swap_in"]
        fiat = [lg for lg in g if lg.kind == "fiat"]
        ts = g[0].ts
        # a 'refund' in an asset the order never escrowed, while it still holds another, is the fill's proceeds
        fill_keys: set[str] = set()
        for lg in g:
            if lg.kind == "escrow_out":
                key = orders.key(lg)
                held = {f for (a, f), dq in self.wallets.p.items() if a == key and dq}
                if held and lg.fam not in held:
                    lg.kind = "swap_in"
                    swap_in.append(lg)
                    fill_keys.add(key)
        # escrow returns first (refunds of placed orders), then the fill consumes what is left
        for lg in g:
            if lg.kind == "escrow_out":
                self._escrow_return(lg, orders)
            elif lg.kind == "escrow_in":
                self.move(lg.ts, lg.acct, orders.key(lg), lg.fam, -lg.amount, f"order {orders.key(lg)}")
                orders.placed(lg)
        consumed: list[tuple[str, str, Decimal, Decimal | None]] = []  # escrow acct, fam, qty, usd
        if orders.keys_for(g) or fill_keys:
            consumed = orders.consume(g, self.wallets, fill_keys)
        fee_usd = sum((lg.usd or D0 for lg in fees), D0)
        for lg in fees:  # fee coins: small disposal at FMV (a net gas refund, e.g. Sui storage rebate, comes in)
            if lg.amount > 0:
                self.acquire(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd or D0, f"fee rebate {lg.tx[:16]}")
            else:
                self.dispose(lg.ts, lg.acct, lg.fam, -lg.amount, lg.usd or D0, "fee", lg.tx)
        if swap_out or swap_in or consumed:
            self._swap(g, swap_out, swap_in, consumed, fiat, fee_usd)
            fee_usd = D0
        for lg in sorted(g, key=lambda x: x.kind != "park_in"):  # park before un-park (stake re-splits)
            k = lg.kind
            if k == "income":
                self.acquire(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd or D0, f"{lg.st} {lg.tx[:16]}")
                self.res.income.append(Income(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd or D0,
                                              "unsolicited_dust" if lg.st.startswith("spam") else lg.st, lg.tx))
            elif k == "acquire" and lg.st == "gift_received" and lg.tx in self.gifts:
                per, start, inherited = self.gifts[lg.tx]
                fmv = lg.usd or D0
                self.acquire(start, lg.acct, lg.fam, lg.amount, lg.amount * per, f"{lg.st} {lg.tx[:16]}", lg.st,
                             gift_fmv=fmv, inherited=inherited)
                what = (f"gift of inherited property: basis ${per:,.2f}/unit (FMV at the date of death "
                        f"{datetime.fromtimestamp(start, UTC):%Y-%m-%d}), always long-term" if inherited else
                        f"gift: donor's basis ${per:,.2f}/unit, holding period from "
                        f"{datetime.fromtimestamp(start, UTC):%Y-%m-%d}")
                if fmv < lg.amount * per:
                    what += (f"; worth ${fmv:,.2f} when received, below that basis, so a sale between the two gives "
                             "no gain or loss and a loss uses the lower value (tax_profile.yaml gifts_received)")
                self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd, what, lg.tx))
            elif k == "acquire":
                self.acquire(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd or D0, f"{lg.st} {lg.tx[:16]}",
                             "" if lg.st in ("exchanger_swap_in", "purchased_item") else lg.st)
                if lg.st == "gift_received":
                    self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd,
                                               "gift received: FMV used as basis (donor's basis unknown; for a "
                                               "later loss the lower of donor basis and FMV applies)", lg.tx))
                elif lg.st not in ("exchanger_swap_in", "purchased_item") and lg.usd and not lg.note:
                    self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, lg.amount, lg.usd,
                                               f"inflow '{lg.sub}' booked as an acquisition at FMV", lg.tx))
            elif k == "dispose":
                self.dispose(lg.ts, lg.acct, lg.fam, -lg.amount, lg.usd or D0, lg.st, lg.tx)
                if lg.note:
                    self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, -lg.amount, lg.usd, lg.note, lg.tx))
            elif k == "gift_out":
                self.dispose(lg.ts, lg.acct, lg.fam, -lg.amount, lg.usd or D0, "gift", lg.tx, gift=True)
            elif k == "futures":
                self._futures(lg)
            elif k == "park_in":
                key = PARK_IN[lg.st].format(chain=lg.chain, fam=lg.fam, acct=lg.acct, dest=lg.sub.partition(":")[2])
                self.move(lg.ts, lg.acct, key, lg.fam, -lg.amount, lg.st)
            elif k == "park_out":
                tmpl, typ = PARK_OUT[lg.st]
                key = tmpl.format(chain=lg.chain, fam=lg.fam, acct=lg.acct)
                have = self.wallets.qty((key, lg.fam))
                back = min(have, lg.amount)
                self.move(lg.ts, key, lg.acct, lg.fam, back, lg.st)
                extra = lg.amount - back
                if extra > 0:
                    price = (lg.usd / lg.amount) if lg.usd else D0
                    self.acquire(lg.ts, lg.acct, lg.fam, extra, extra * price, f"{typ} {lg.tx[:16]}")
                    self.res.income.append(Income(lg.ts, lg.acct, lg.fam, extra, extra * price, typ, lg.tx))
            elif k in ("move_out", "move_in") and id(lg) not in paired:
                self._transit(lg, transit)

    def _transit(self, lg: Leg, transit: dict[str, deque[Lot]]) -> None:
        dq = transit[lg.fam]
        if lg.amount < 0:
            for lot in self.wallets.take((lg.acct, lg.fam), -lg.amount, lg.ts, f"{lg.sub} {lg.tx[:16]}"):
                lot.origin = f"{lg.acct} {lg.sub} {_date(lg.ts)} {lg.tx[:16]}"
                dq.append(lot)
            return
        need = lg.amount
        while need > 0 and dq:
            lot = dq[0]
            part = dq.popleft() if lot.qty <= need else lot.split(need)
            need -= part.qty
            self.wallets.add((lg.acct, lg.fam), part)
        if need > Decimal("1e-12"):
            price = (lg.usd / lg.amount) if lg.usd else D0
            self.acquire(lg.ts, lg.acct, lg.fam, need, need * price, f"unmatched inflow {lg.tx[:16]}", "unknown_source")
            if need * price >= 1:
                self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, need, need * price,
                                           f"inflow '{lg.sub}' with no matching outflow: basis = FMV on arrival", lg.tx))

    def _swap(self, g: list[Leg], outs: list[Leg], ins: list[Leg], consumed: list[tuple[str, str, Decimal, Decimal | None]],
              fiat: list[Leg], fee_usd: Decimal) -> None:
        net_fiat = sum((lg.amount for lg in fiat), D0)  # USD received minus USD paid (incl. USD fees)
        in_usd = [lg.usd for lg in ins]
        out_usd = [lg.usd for lg in outs] + [c[3] for c in consumed]
        has_out = bool(outs or consumed)
        if fiat and ins and not has_out:  # bought with USD: cost = USD paid incl. fees
            value = max(-net_fiat, D0)
        elif fiat and has_out and not ins:  # sold for USD: proceeds = USD received net of USD fees
            value = max(net_fiat, D0)
        elif ins and all(u for u in in_usd):
            value = sum((u or D0 for u in in_usd), D0)
        else:
            value = sum((u or D0 for u in out_usd), D0)
        # disposals: proceeds share by FMV weight (or quantity when unpriced); fees reduce proceeds if nothing acquired
        out_items = [(lg.acct, lg.fam, -lg.amount, lg.usd) for lg in outs] + list(consumed)
        proceeds_total = max(value - (fee_usd if not ins else D0), D0)
        wsum = sum((u or D0 for *_, u in out_items), D0)
        unpriced = [x for x in out_items if not x[3]]
        if unpriced and wsum and ins and all(u for u in in_usd):
            # the received value is known: priced items keep their FMV, unpriced ones (e.g. an LP token being
            # redeemed) take the rest, by quantity
            priced_total = min(wsum, proceeds_total)
            rest = proceeds_total - priced_total
            uq = sum((x[2] for x in unpriced), D0)
            for acct, fam, q, u in out_items:
                p = priced_total * (u / wsum) if u else (rest * q / uq if uq else rest / len(unpriced))
                self.dispose(g[0].ts, acct, fam, q, p, "trade", g[0].tx)
            out_items = []
        for acct, fam, q, u in out_items:
            share = (u or D0) / wsum if wsum else Decimal(1) / len(out_items)
            self.dispose(g[0].ts, acct, fam, q, proceeds_total * share, "trade", g[0].tx)
        basis_total = value + fee_usd if ins else D0
        isum = sum((u or D0 for u in in_usd), D0)
        for lg in ins:
            share = (lg.usd or D0) / isum if isum else Decimal(1) / len(ins)
            self.acquire(lg.ts, lg.acct, lg.fam, lg.amount, basis_total * share, f"trade {lg.tx[:16]}")

    def _futures(self, lg: Leg) -> None:
        usd = lg.usd or D0
        if lg.amount > 0:
            self.acquire(lg.ts, lg.acct, lg.fam, lg.amount, usd, f"futures pnl {lg.tx[:16]}")
            self.res.futures.append(Income(lg.ts, lg.acct, lg.fam, lg.amount, usd, lg.st, lg.tx))
        else:  # loss/fee settled in coin: the coin is disposed at FMV, the contract loss is the FMV
            self.dispose(lg.ts, lg.acct, lg.fam, -lg.amount, usd, "futures_settlement", lg.tx)
            self.res.futures.append(Income(lg.ts, lg.acct, lg.fam, lg.amount, -usd, lg.st, lg.tx))

    def _escrow_return(self, lg: Leg, orders: OrderBook) -> None:
        key = orders.key(lg)
        have = self.wallets.qty((key, lg.fam))
        back = min(have, lg.amount)
        self.move(lg.ts, key, lg.acct, lg.fam, back, "order refund")
        if lg.amount - back > Decimal("1e-12"):
            extra = lg.amount - back
            price = (lg.usd / lg.amount) if lg.usd else D0
            self.acquire(lg.ts, lg.acct, lg.fam, extra, extra * price, f"order return {lg.tx[:16]}", "order_return_surplus")
            if extra * price >= 1:
                self.res.flags.append(Flag(lg.ts, lg.acct, lg.fam, extra, extra * price,
                                           "order refund larger than the escrowed amount: surplus at FMV", lg.tx))


class OrderBook:
    """Escrow per order. A fill consumes the escrow that no later refund will return, split across the
    order's fills by the value each fill paid out."""

    def __init__(self, legs: list[Leg]) -> None:
        self.by_tx: dict[str, str] = {}
        self.settled_by: dict[str, set[str]] = defaultdict(set)  # fill tx -> orders whose placement names it
        for lg in legs:
            if lg.kind == "escrow_in":
                self.by_tx.setdefault(lg.tx, lg.order or lg.tx)
        for lg in legs:
            if lg.kind == "escrow_in" and lg.linked:
                self.settled_by[lg.linked].add(self.key(lg))
        self.returns: dict[str, list[tuple[int, int, str, Decimal]]] = defaultdict(list)
        self.price: dict[tuple[str, str], Decimal] = {}
        fill_value: dict[str, dict[str, Decimal]] = defaultdict(dict)  # order -> fill tx -> value paid out
        for lg in legs:
            k = self.key(lg)
            if lg.kind == "escrow_out":
                self.returns[k].append((lg.ts, lg.i, lg.fam, lg.amount))
            if self.settles(lg):
                fv = fill_value[k]
                fv[lg.tx] = fv.get(lg.tx, D0) + (lg.usd or D0 if lg.kind == "swap_in" else D0)
        self.fill_value = fill_value
        self.done: dict[str, set[str]] = defaultdict(set)

    def settles(self, lg: Leg) -> bool:
        """A leg of a tx that fills an order (an LP deposit/withdraw can be the fill of an lp_add/lp_remove order)."""
        if lg.cls == "ORDER_FILLED":
            return True
        return lg.cls in ("LP_DEPOSIT", "LP_WITHDRAW", "SWAP") and (lg.tx in self.settled_by or lg.linked in self.by_tx)

    def keys_for(self, g: list[Leg]) -> set[str]:
        keys = {self.key(lg) for lg in g if self.settles(lg)}
        for lg in g:
            keys |= self.settled_by.get(lg.tx, set())
        return keys

    def key(self, lg: Leg) -> str:
        k = lg.order or self.by_tx.get(lg.linked, "") or self.by_tx.get(lg.tx, "") or lg.linked or lg.tx
        return f"escrow:{k}"

    def placed(self, lg: Leg) -> None:
        if lg.usd:
            self.price[(self.key(lg), lg.fam)] = lg.usd / -lg.amount

    def consume(self, g: list[Leg], wallets: Pools, extra: set[str] | None = None
                ) -> list[tuple[str, str, Decimal, Decimal | None]]:
        out: list[tuple[str, str, Decimal, Decimal | None]] = []
        for k in sorted(self.keys_for(g) | (extra or set())):
            ts, tx = g[0].ts, g[0].tx
            fv = self.fill_value.get(k, {})
            left = {t: v for t, v in fv.items() if t not in self.done[k]}
            self.done[k].add(tx)
            tot = sum(left.values(), D0)
            share = (left.get(tx, D0) / tot) if tot else Decimal(1) / max(1, len(left))
            for (acct, fam), dq in list(wallets.p.items()):
                if acct != k or not dq:
                    continue
                have = sum((x.qty for x in dq), D0)
                later = sum((q for t, _, f, q in self.returns.get(k, []) if f == fam and t > ts), D0)
                q = max(have - later, D0) * share
                if q > 0:
                    p = self.price.get((k, fam))
                    out.append((k, fam, q, None if p is None else p * q))
        return out


def pair_moves(legs: list[Leg]) -> list[tuple[Leg, Leg]]:
    """Match own-wallet moves: same tx (other account, same family), then linked tx ids."""
    outs = [lg for lg in legs if lg.kind == "move_out"]
    ins = [lg for lg in legs if lg.kind == "move_in"]
    by_tx: dict[tuple[str, str], list[Leg]] = defaultdict(list)
    by_hash: dict[str, list[Leg]] = defaultdict(list)
    for i in ins:
        by_tx[(i.chain, i.tx)].append(i)
        by_hash[i.tx].append(i)
    used: set[int] = set()
    pairs: list[tuple[Leg, Leg]] = []
    for o in outs:
        cands = [i for i in by_tx.get((o.chain, o.tx), []) if i.fam == o.fam and id(i) not in used and i.acct != o.acct]
        cands += [i for h in (o.linked,) if h for i in by_hash.get(h, []) if i.fam == o.fam and id(i) not in used]
        cands += [i for i in ins if o.tx and i.linked == o.tx and i.fam == o.fam and id(i) not in used]
        cands = [i for i in cands if i.amount <= -o.amount * Decimal("1.02")]
        if cands:
            best = min(cands, key=lambda i: (abs(i.amount + o.amount), abs(i.ts - o.ts)))
            used.add(id(best))
            pairs.append((o, best))
    # same asset into another account within two days, either order (exchange clocks drift), 98-100%
    done = {id(o) for o, _ in pairs}
    by_fam: dict[str, list[Leg]] = defaultdict(list)
    for i in ins:
        if id(i) not in used:
            by_fam[i.fam].append(i)
    for o in outs:
        if id(o) in done:
            continue
        q = -o.amount
        cands = [i for i in by_fam.get(o.fam, []) if id(i) not in used
                 and abs(i.ts - o.ts) <= 2 * 86400 and q * Decimal("0.98") <= i.amount <= q * Decimal("1.0001")]
        if cands:
            best = min(cands, key=lambda i: (abs(i.amount - q) / q, abs(i.ts - o.ts)))
            used.add(id(best))
            pairs.append((o, best))
    return pairs


def swap_services(legs: list[Leg], paired: set[int]) -> int:
    """Unpaired move out + move in of a different asset within 6h at a similar USD value: an instant-swap
    service (assumed). Becomes a disposal and an acquisition at FMV."""
    outs = [lg for lg in legs if lg.kind == "move_out" and id(lg) not in paired and lg.usd]
    ins = [lg for lg in legs if lg.kind == "move_in" and id(lg) not in paired and lg.usd]
    used: set[int] = set()
    n = 0
    for o in outs:
        best: Leg | None = None
        for i in ins:
            if id(i) in used or i.fam == o.fam or not (o.ts - 600 <= i.ts <= o.ts + 6 * 3600):
                continue
            r = (i.usd or D0) / (o.usd or Decimal(1))
            if Decimal("0.95") <= r <= Decimal("1.03") and (best is None or abs(i.ts - o.ts) < abs(best.ts - o.ts)):
                best = i
        if best is not None:
            used.add(id(best))
            o.kind, best.kind = "dispose", "acquire"
            o.note = best.note = (f"assumed swap via a service: {-o.amount:f} {o.fam.upper()} ({o.acct}) -> "
                                  f"{best.amount:f} {best.fam.upper()} ({best.acct})")
            n += 1
    return n


# ------------------------------------------------------------------------------------------ report


def _year(ts: int) -> int:
    return datetime.fromtimestamp(ts, UTC).year


def _m(d: Decimal) -> str:
    return f"${d:,.2f}" if d >= 0 else f"-${-d:,.2f}"


def write_reports(res: Result, out_dir: Path, prefix: str = "basis") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {k: out_dir / f"{prefix}_{k}.csv" for k in ("disposals", "income", "futures", "gifts", "allocation_2025",
                                                         "holdings", "flags")}
    paths["summary"] = out_dir / f"{prefix}_summary.md"

    def disp_rows(ds: list[Disposal]) -> list[list[str]]:
        return [[_date(d.ts), d.acct, d.fam.upper(), f"{d.lot.qty:f}", _date(d.lot.acq), f"{d.proceeds:.2f}",
                 f"{d.lot.cost:.2f}", f"{d.gain:.2f}", d.term, d.event, d.method, d.tx, d.lot.origin, d.lot.flag]
                for d in ds]

    hdr = ["date_sold", "account", "asset", "quantity", "date_acquired", "proceeds_usd", "cost_basis_usd", "gain_usd",
           "term", "event", "method", "tx", "lot_origin", "flag"]
    for key, ds in (("disposals", res.disposals), ("gifts", res.gifts)):
        with open(paths[key], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(hdr if key == "disposals" else [h.replace("proceeds", "fmv").replace("gain", "fmv_minus_basis")
                                                       for h in hdr])
            w.writerows(disp_rows(sorted(ds, key=lambda d: d.ts)))
    for key, inc in (("income", res.income), ("futures", res.futures)):
        with open(paths[key], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "account", "asset", "quantity", "usd", "type", "tx"])
            w.writerows([[_date(x.ts), x.acct, x.fam.upper(), f"{x.qty:f}", f"{x.usd:.2f}", x.typ, x.tx]
                         for x in sorted(inc, key=lambda x: x.ts)])
    with open(paths["allocation_2025"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["account", "asset", "quantity", "cost_basis_usd", "date_acquired", "how", "lot_origin"])
        w.writerows([[a, fam.upper(), f"{lot.qty:f}", f"{lot.cost:.2f}", _date(lot.acq), how, lot.origin]
                     for a, fam, lot, how in sorted(res.allocation, key=lambda x: (x[0], x[1], x[2].acq))])
        w.writerows([["(unallocated: more basis than units held)", fam.upper(), f"{lot.qty:f}", f"{lot.cost:.2f}",
                      _date(lot.acq), "unallocated", lot.origin] for fam, lot in res.unallocated])
    with open(paths["holdings"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["account", "asset", "quantity", "cost_basis_usd", "date_acquired", "lot_origin", "flag"])
        for (a, fam), lots in sorted(res.holdings.items()):
            w.writerows([[a, fam.upper(), f"{lot.qty:f}", f"{lot.cost:.2f}", _date(lot.acq), lot.origin, lot.flag]
                         for lot in lots if lot.qty > 0])
    with open(paths["flags"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "account", "asset", "quantity", "usd", "issue", "tx"])
        w.writerows([[_date(x.ts), x.acct, x.fam.upper(), f"{x.qty:f}", "" if x.usd is None else f"{x.usd:.2f}",
                      x.what, x.tx] for x in sorted(res.flags, key=lambda x: x.ts)])

    years = sorted({_year(d.ts) for d in res.disposals} | {_year(x.ts) for x in res.income}
                   | {_year(x.ts) for x in res.futures})
    L = ["# Cost basis summary", "",
         f"Universal {res.methods[0].upper()} through 2024-12-31; from 2025-01-01 {res.methods[1].upper()} per "
         "wallet/exchange account, after allocating the unused basis to the wallets that held the units (Rev. Proc. 2024-28 safe harbor). Fees paid in crypto are "
         "small disposals at FMV, added to the basis of what the same transaction bought (or deducted from proceeds). "
         "Not tax advice: check against the IRS texts and with a tax professional before filing.", "",
         "| year | short-term gain | long-term gain | proceeds | income | futures P&L | gifts given (FMV) |",
         "|---|---:|---:|---:|---:|---:|---:|"]
    for y in years:
        ds = [d for d in res.disposals if _year(d.ts) == y]
        st = sum((d.gain for d in ds if d.term == "short"), D0)
        lt = sum((d.gain for d in ds if d.term == "long"), D0)
        pr = sum((d.proceeds for d in ds), D0)
        inc_usd = sum((x.usd for x in res.income if _year(x.ts) == y), D0)
        fut = sum((x.usd for x in res.futures if _year(x.ts) == y), D0)
        gf = sum((d.proceeds for d in res.gifts if _year(d.ts) == y), D0)
        L.append(f"| {y} | {_m(st)} | {_m(lt)} | {_m(pr)} | {_m(inc_usd)} | {_m(fut)} | {_m(gf)} |")
    L += ["", "## Income by type", "", "| year | type | USD |", "|---|---|---:|"]
    by: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for x in res.income:
        by[(_year(x.ts), x.typ)] += x.usd
    L += [f"| {y} | {t} | {_m(v)} |" for (y, t), v in sorted(by.items()) if v >= Decimal("0.005")]
    alloc_basis = sum((lot.cost for *_, lot, _h in res.allocation), D0)
    L += ["", "## 2025-01-01 allocation", "",
          f"{len(res.allocation)} lot pieces, {_m(alloc_basis)} of basis allocated "
          f"({sum(1 for *_, h in res.allocation if h == 'traced')} traced to the wallet holding the same lot, "
          f"{sum(1 for *_, h in res.allocation if h == 'assigned')} assigned to remaining holdings, "
          f"{sum(1 for *_, h in res.allocation if h == 'no basis left')} holdings with no basis left). "
          f"Unallocated basis: {_m(sum((lot.cost for _, lot in res.unallocated), D0))}. Details: `{paths['allocation_2025'].name}`.",
          "", "## Holdings and their basis at the end of the history", "",
          "| account | assets (quantity) | cost basis |", "|---|---|---:|"]
    acct_basis: dict[str, Decimal] = defaultdict(Decimal)
    acct_assets: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for (a, fam), lots in res.holdings.items():
        if "@" in fam:
            continue
        for lot in lots:
            acct_basis[a] += lot.cost
            acct_assets[a][fam] += lot.qty
    parked = ("escrow:", "staked:", "deposit:", "xp-chain:", "vault:", "offlist:")
    for a in sorted(acct_basis, key=lambda a: -acct_basis[a]):
        if a.startswith(parked) or acct_basis[a] < 1:
            continue
        top = sorted(acct_assets[a].items(), key=lambda kv: -kv[1])[:4]
        L.append(f"| {a} | {', '.join(f'{f.upper()} {q:,.4f}' for f, q in top)} | {_m(acct_basis[a])} |")
    L += ["", "Parked outside the wallets (open/unresolved orders, stake, off-list chains):", ""]
    for a in sorted((a for a in acct_basis if a.startswith(parked)), key=lambda a: -acct_basis[a]):
        if acct_basis[a] >= 1:
            L.append(f"- `{a[:70]}`: {', '.join(f'{f.upper()} {q:,.4f}' for f, q in acct_assets[a].items())} "
                     f"({_m(acct_basis[a])} basis)")
    L += ["", "## Items to review", ""]
    cnt: dict[str, list[Flag]] = defaultdict(list)
    for fl in res.flags:
        cnt[fl.what.split(":")[0]].append(fl)
    for what, fls in sorted(cnt.items(), key=lambda kv: -sum((f.usd or D0 for f in kv[1]), D0)):
        L.append(f"- {what}: {len(fls)} item(s), {_m(sum((f.usd or D0 for f in fls), D0))}")
    L += ["", f"Full list: `{paths['flags'].name}`."]
    paths["summary"].write_text("\n".join(L) + "\n")
    return paths
