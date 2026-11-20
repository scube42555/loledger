"""CoinLedger comparison: a validation report only. CoinLedger data never feeds classified.csv.

Reads the CoinLedger export and our classified.csv, groups CoinLedger rows into events (Internal Id),
matches them to our transactions (1. tx id, 2. asset+amount+time for rows without ids), and reports
coverage, classification agreement, amount differences, income totals and what each side lacks.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .registry import norm_symbol

PLATFORM_CHAIN = {
    "Gemini": "Gemini", "MEXC": "MEXC", "Solana": "Solana", "Cardano": "Cardano", "Ethereum": "Ethereum",
    "Avalanche C-Chain": "Avalanche", "Sui": "Sui", "Crypto.com": "Crypto.com", "Algorand": "Algorand",
    "Hedera Hashgraph": "Hedera", "Binance US": "Binance.US", "Cosmos": "Cosmos", "Injective": "Injective",
    "Kaspa": "Kaspa", "Bitcoin": "Bitcoin", "Doge": "Doge", "Manual": "Chia", "Polygon": "Polygon",
    "Arbitrum": "Arbitrum", "Coinbase": "Coinbase", "OKX": "OKX",
}
EXCHANGES = {"Gemini", "MEXC", "Crypto.com", "Binance.US", "Coinbase", "OKX"}

CL_FAMILY = {
    "Trade": "trade", "Buy": "trade", "Sell": "trade", "Multi Asset Swap": "trade",
    "Deposit": "transfer", "Withdrawal": "transfer", "Payment": "transfer", "Transfer": "internal",
    "Bridge": "bridge", "Staking Income": "income", "Mining Income": "income", "Native Stake": "stake",
    "Add Liquidity": "lp", "Remove Liquidity": "lp", "Mint": "mint", "Outgoing Gift": "gift", "Fee": "fee",
    "Basis Reallocation": "other", "Manual Position": "other", "Uncategorized": "other",
}
INCOME_SUBTYPES = ("staking_reward", "pool_reward", "beacon_reward", "proposer_reward", "farming_reward", "exchange_credit",
                   "staking_reward_withdrawal", "protocol_reward", "governance_reward", "beacon_withdrawal",
                   "validator_reward")


def our_family(cls: str, subtype: str) -> str:
    st = subtype.split(":")[0]
    if cls in ("SWAP", "ORDER_FILLED") or st == "exchange_trade":
        return "trade"
    if cls in ("ORDER_PLACED", "ORDER_CANCELLED"):
        return "order"
    if cls == "INTERNAL_TRANSFER":
        return "internal"
    if cls.startswith("LP_"):
        return "lp"
    if cls == "TRANSFER":
        return "bridge" if st.startswith("bridge") else "transfer"
    if st.startswith(INCOME_SUBTYPES) or st in INCOME_SUBTYPES:
        return "income"
    if st.startswith("spam") or st == "airdrop":
        return "spam"
    if st.startswith("bridge"):
        return "bridge"
    if st in ("stake", "unstake_or_reward", "stake_management", "yield_deposit", "yield_withdraw"):
        return "stake"
    if st in ("gift_received", "gift_sent", "promotional_gift"):
        return "gift"
    if st in ("mint", "burn"):
        return "mint"
    return "other"


def canon_id(chain: str, txid: str) -> str:
    t = (txid or "").strip()
    body = t[2:] if t.lower().startswith("0x") else t
    if body and all(c in "0123456789abcdefABCDEF" for c in body):
        return body.lower()
    return t  # base58 / base32 / Hedera ids are kept as-is


def _dec(s: str) -> Decimal:
    try:
        return Decimal((s or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


WRAPPED = {"wavax": "avax", "weth": "eth", "wsol": "sol", "wbtc": "btc", "matic": "pol"}  # MATIC was renamed POL


def _sym(s: str) -> str:
    n = norm_symbol(s or "")
    return WRAPPED.get(n, n)


Key = tuple[str, str, str]  # (chain, tx_hash, wallet)

# CoinLedger type -> our family pairs that differ only because CoinLedger has no such category
EXPECTED = {
    ("Outgoing Gift", "internal"): ("CoinLedger gift between the user's own wallets (user-confirmed: the earlier "
                                    "round of gifts was reclaimed before being sent out again)"),
    ("Outgoing Gift", "transfer"): ("CoinLedger gift not sent to a listed gift recipient (user-confirmed: earlier gifts "
                                    "were reclaimed; only addresses labelled Gift in addresses.csv received gifts)"),
    ("Deposit", "spam"): "we flag the inflow as spam/scam; CoinLedger books it as a deposit",
    ("Withdrawal", "spam"): "fake-token or poisoning 'outflow' (nothing really left); CoinLedger books a withdrawal",
    ("Mint", "spam"): "unsolicited NFT; CoinLedger books a mint",
    ("Withdrawal", "order"): "funds sent to a DEX order/escrow script; CoinLedger sees a plain withdrawal",
    ("Deposit", "order"): "order refund/cancel; CoinLedger sees a plain deposit",
    ("Deposit", "trade"): "order fill paid by a batcher/solver; CoinLedger sees a plain deposit",
    ("Withdrawal", "trade"): "order placement/fill leg; CoinLedger sees a plain withdrawal",
    ("Deposit", "internal"): "we know the sender is your own wallet/exchange; CoinLedger left it a deposit",
    ("Withdrawal", "internal"): "we know the recipient is your own wallet/exchange",
    ("Deposit", "bridge"): "bridge leg", ("Withdrawal", "bridge"): "bridge leg", ("Bridge", "transfer"): "bridge leg",
    ("Deposit", "stake"): "stake account movement", ("Withdrawal", "stake"): "stake account movement",
    ("Native Stake", "trade"): "liquid-staking swap", ("Mint", "lp"): "LP token mint = LP deposit",
    ("Remove Liquidity", "trade"): "LP exit routed through a swap", ("Add Liquidity", "trade"): "LP entry via zap",
    ("Uncategorized", "trade"): "CoinLedger left it uncategorized", ("Uncategorized", "lp"): "uncategorized in CoinLedger",
    ("Uncategorized", "internal"): "uncategorized in CoinLedger", ("Manual Position", "trade"): "manual entry",
    ("Deposit", "income"): "reward we recognise as income", ("Transfer", "transfer"): "transfer",
    ("Native Unstake", "other"): "unstake", ("Native Stake", "income"): "CoinLedger pairs each reward with a re-stake entry", ("Trade", "other"): "wrap/unwrap (not a taxable trade for us)",
}


@dataclass
class CLEvent:
    iid: str
    ts: int
    type: str
    platform: str
    chain: str
    account: str
    txid: str
    legs: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))  # symbol -> signed (fees incl.)
    fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    match: str = ""  # tx_id | amount_time | exchange_daily | ""
    our_key: Key | None = None


@dataclass
class OurTx:
    chain: str
    tx_hash: str
    ts: int
    wallet: str
    label: str
    legs: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    families: Counter[str] = field(default_factory=Counter)
    classes: Counter[str] = field(default_factory=Counter)
    net_with_fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    usd: Decimal = Decimal(0)
    matched: bool = False
    legs_claimed: bool = False  # matched by an event with asset legs (a fee-only event leaves the legs free)


def load_coinledger(path: Path) -> list[CLEvent]:
    events: dict[str, CLEvent] = {}
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        iid = r["Internal Id"] or f"row{len(events)}"
        ts = int(datetime.fromisoformat(r["Timestamp (UTC)"].replace("Z", "")).replace(tzinfo=UTC).timestamp())
        e = events.get(iid)
        if e is None:
            e = events[iid] = CLEvent(iid, ts, r["Type"], r["Platform"], PLATFORM_CHAIN.get(r["Platform"], r["Platform"]),
                                      r["Account Name"], r["Blockchain Id"].strip())
        e.txid = e.txid or r["Blockchain Id"].strip()
        amt, rt, sym = _dec(r["Amount"]), r["Record Type"], _sym(r["Asset"])
        if rt == "Credit":
            e.legs[sym] += abs(amt)
        elif rt == "Debit":
            e.legs[sym] -= abs(amt)
        elif rt in ("Platform Fee", "Network Fee"):
            e.fees[sym] += abs(amt)
    return sorted(events.values(), key=lambda e: e.ts)


def load_ours(path: Path) -> dict[Key, OurTx]:
    out: dict[Key, OurTx] = {}
    for r in csv.DictReader(path.open(encoding="utf-8")):
        key = (r["chain"], r["tx_hash"], r["wallet"])
        t = out.get(key)
        if t is None:
            t = out[key] = OurTx(r["chain"], r["tx_hash"], int(r["timestamp"]), r["wallet"], r["wallet_label"])
        fam = our_family(r["classification"], r["subtype"])
        t.families[fam] += 1
        t.classes[f"{r['classification']}/{r['subtype'].split(':')[0]}"] += 1
        if r["leg_role"] != "fee" and r["leg_id"] != "gas":
            t.legs[_sym(r["token"])] += _dec(r["amount"])
        t.net_with_fees[_sym(r["token"])] += _dec(r["amount"])
        t.usd += abs(_dec(r["usd_value_at_time"]))
    return out


def _lab(s: str) -> str:
    return " ".join(s.lower().split())


def _agreement(e: CLEvent, t: OurTx) -> int:
    return sum(1 for k, v in e.legs.items() if v and _close(v, t.legs.get(k, Decimal(0))))


def compare(cl: list[CLEvent], ours: dict[Key, OurTx]) -> None:
    by_id: dict[tuple[str, str], list[Key]] = defaultdict(list)
    by_chain: dict[str, list[OurTx]] = defaultdict(list)
    for key, t in ours.items():
        by_id[(t.chain, canon_id(t.chain, t.tx_hash))].append(key)
        if t.chain == "Hedera":  # CoinLedger uses the consensus timestamp ("1737076684.533190…") as the id
            by_id[(t.chain, f"ts:{t.ts}")].append(key)
        by_chain[t.chain].append(t)
    # 1. tx id; several of our wallets can share a tx (own-wallet transfer): pick the account/legs that agree
    for e in cl:
        if not e.txid or e.chain in EXCHANGES:
            continue
        cands = by_id.get((e.chain, canon_id(e.chain, e.txid)), [])
        if not cands and e.chain == "Hedera" and re.fullmatch(r"\d{9,11}\.\d+", e.txid):
            cands = by_id.get((e.chain, f"ts:{e.txid.split('.')[0]}"), [])
        if cands:
            k = max(cands, key=lambda k: (_lab(ours[k].label) == _lab(e.account), _agreement(e, ours[k]),
                                          not ours[k].matched))
            e.match, e.our_key = "tx_id", k
            ours[k].matched = True
            ours[k].legs_claimed |= any(e.legs.values())
    # 2. on-chain events without ids: asset + amount (+-0.5%) + time (+-2h; manual entries are often day-precision)
    for e in cl:
        if e.match or not e.legs or e.chain in EXCHANGES:
            continue
        window = 36 * 3600 if e.platform == "Manual" else 3 * 86400 if CL_FAMILY.get(e.type) == "income" else 2 * 3600
        best: tuple[float, OurTx] | None = None
        for t in by_chain.get(e.chain, []):
            if t.legs_claimed or abs(t.ts - e.ts) > window:
                continue
            if all(_close(v, t.legs.get(k, Decimal(0))) for k, v in e.legs.items() if v):
                if best is None or abs(t.ts - e.ts) < best[0]:
                    best = (abs(t.ts - e.ts), t)
        if best:
            t = best[1]
            e.match, e.our_key = "amount_time", (t.chain, t.tx_hash, t.wallet)
            t.matched = t.legs_claimed = True


def exchange_daily(cl: list[CLEvent], ours: dict[Key, OurTx], lo: int, hi: int
                   ) -> list[tuple[str, str, str, Decimal, Decimal]]:
    """(platform, day, asset, CoinLedger net, our net) for every exchange day/asset, fees included on both sides."""
    a: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    b: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for e in cl:
        if e.chain in EXCHANGES:
            for k, v in e.legs.items():
                a[(e.chain, _d(e.ts), k)] += v
            for k, v in e.fees.items():
                a[(e.chain, _d(e.ts), k)] -= v
    for t in ours.values():
        if t.chain in EXCHANGES and lo <= t.ts <= hi + 86400:
            for k, v in t.net_with_fees.items():
                b[(t.chain, _d(t.ts), k)] += v
    return [(k[0], k[1], k[2], a.get(k, Decimal(0)), b.get(k, Decimal(0))) for k in sorted(set(a) | set(b))
            if a.get(k, Decimal(0)) or b.get(k, Decimal(0))]


def _close(a: Decimal, b: Decimal) -> bool:
    if a == 0 or b == 0:
        return a == b
    if (a > 0) != (b > 0):
        return False
    return abs(a - b) <= abs(a) * Decimal("0.005") + Decimal("1e-9")


def _d(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _legs(d: dict[str, Decimal]) -> str:
    return ", ".join(f"{k.upper()} {v.normalize():f}" for k, v in d.items() if v) or "—"


def _year(ts: int) -> str:
    return str(datetime.fromtimestamp(ts, UTC).year)


def report(cl: list[CLEvent], ours: dict[Key, OurTx], md: Path, diff_csv: Path) -> dict[str, int]:
    compare(cl, ours)
    lo, hi = min(e.ts for e in cl), max(e.ts for e in cl)
    cl_chains = {e.chain for e in cl}
    covered = {t.chain for t in ours.values()}
    L: list[str] = []
    w = L.append
    w("# CoinLedger comparison (validation only)\n")
    w(f"CoinLedger export: {len(cl)} events, {_d(lo)} → {_d(hi)}. Our classified.csv: {len(ours)} wallet-transactions. "
      "CoinLedger is never used as a data source; this report only compares the two.\n")

    # ---- on-chain coverage
    onchain = [e for e in cl if e.chain not in EXCHANGES]
    ours_only: dict[str, list[OurTx]] = defaultdict(list)
    for t in ours.values():
        if not t.matched and lo <= t.ts <= hi and t.chain in cl_chains and t.chain not in EXCHANGES and not _ignorable(t):
            ours_only[t.chain].append(t)
    w("## On-chain coverage\n")
    w("| chain | CoinLedger events | matched by tx id | matched by amount/time | only in CoinLedger | only in ours* |")
    w("|---|---:|---:|---:|---:|---:|")
    cov: dict[str, Counter[str]] = defaultdict(Counter)
    for e in onchain:
        cov[e.chain][e.match or "cl_only"] += 1
    for ch in sorted(cov, key=lambda c: -sum(cov[c].values())):
        c = cov[ch]
        note = "" if ch in covered else " (no adapter)"
        w(f"| {ch}{note} | {sum(c.values())} | {c['tx_id']} | {c['amount_time']} | {c['cl_only']} | {len(ours_only.get(ch, []))} |")
    w("\n\\* inside CoinLedger's date range; spam, dust, approvals and gas-only rows left out.\n")

    # ---- exchanges
    days = exchange_daily(cl, ours, lo, hi)
    w("## Exchanges: daily net flow per asset (fees included)\n")
    w("CoinLedger splits orders into fills and we group them per order, so exchanges are compared per day and asset.\n")
    w("| exchange | day-asset pairs | agree (±0.5%) | differ | CoinLedger only | ours only |")
    w("|---|---:|---:|---:|---:|---:|")
    ex_stats: dict[str, Counter[str]] = defaultdict(Counter)
    ex_bad: list[tuple[str, str, str, Decimal, Decimal]] = []
    for row in days:
        p, _, _, a, b = row
        k = "agree" if _close(a, b) or abs(a - b) < Decimal("1e-6") else "cl_only" if not b else "ours_only" if not a else "differ"
        ex_stats[p][k] += 1
        if k != "agree":
            ex_bad.append(row)
    for p in sorted(ex_stats, key=lambda p: -sum(ex_stats[p].values())):
        c = ex_stats[p]
        w(f"| {p} | {sum(c.values())} | {c['agree']} | {c['differ']} | {c['cl_only']} | {c['ours_only']} |")
    if ex_bad:
        w("\nLargest differences (all listed in the CSV):\n")
        w("| exchange | day | asset | CoinLedger | ours |")
        w("|---|---|---|---:|---:|")
        for p, d, k, a, b in sorted(ex_bad, key=lambda r: -abs(r[3] - r[4]) * (1 if r[2] in ("usd", "usdt", "usdc") else 100))[:30]:
            w(f"| {p} | {d} | {k.upper()} | {a.normalize():f} | {b.normalize():f} |")

    # ---- classification
    agree: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[CLEvent]] = defaultdict(list)
    for e in onchain:
        if e.our_key and e.legs and any(e.legs.values()):
            fam = ours[e.our_key].families.most_common(1)[0][0]
            pair = (e.type, fam)
            agree[pair] += 1
            examples[pair].append(e)
    same = sum(n for pair, n in agree.items() if CL_FAMILY.get(pair[0], "other") == pair[1])
    expected = {pair: n for pair, n in agree.items() if CL_FAMILY.get(pair[0], "other") != pair[1] and pair in EXPECTED}
    review = {pair: n for pair, n in agree.items() if CL_FAMILY.get(pair[0], "other") != pair[1] and pair not in EXPECTED}
    w("\n## Classification of matched on-chain events\n")
    w(f"{sum(agree.values())} matched events with asset movement: {same} same category, {sum(expected.values())} "
      f"differ only because CoinLedger lacks the category, {sum(review.values())} worth reviewing.\n")
    if expected:
        w("**Expected differences**\n")
        w("| CoinLedger type | ours | count | why |")
        w("|---|---|---:|---|")
        for pair, n in sorted(expected.items(), key=lambda x: -x[1]):
            w(f"| {pair[0]} | {pair[1]} | {n} | {EXPECTED[pair]} |")
    if review:
        w("\n**Worth reviewing** (up to 3 examples each; all in the CSV)\n")
        for pair, n in sorted(review.items(), key=lambda x: -x[1]):
            w(f"- **CoinLedger {pair[0]} → ours {pair[1]}** ({n})")
            for e in examples[pair][:3]:
                ot = ours[e.our_key] if e.our_key else None
                w(f"  - {_d(e.ts)} {e.chain} `{(e.txid or e.iid)[:16]}…` CoinLedger: {_legs(e.legs)} — ours: "
                  f"{', '.join(ot.classes) if ot else ''}")

    # ---- amounts
    diffs = []
    for e in onchain:
        if not e.our_key:
            continue
        t = ours[e.our_key]
        if t.families.get("spam"):
            continue
        for k, v in e.legs.items():
            if k == "unknownsymbol" or (len(k) > 12 and all(c in "0123456789abcdef" for c in k)):
                continue  # CoinLedger could not name the token; symbols can't be compared
            if v and not _close(v, t.legs.get(k, Decimal(0))):
                diffs.append((e, k, v, t.legs.get(k, Decimal(0))))
    w(f"\n## Amount differences on matched on-chain events (non-spam, > 0.5%): {len(diffs)}\n")
    for e, k, v, o in diffs[:30]:
        w(f"- {_d(e.ts)} {e.chain} {e.type} `{(e.txid or e.iid)[:16]}…` {k.upper()}: CoinLedger {v.normalize():f} vs ours {o.normalize():f}")

    # ---- income
    inc_cl: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for e in cl:
        if CL_FAMILY.get(e.type) == "income":
            for k, v in e.legs.items():
                if v > 0:
                    inc_cl[(e.chain, _year(e.ts), k.upper())] += v
    inc_ours: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for t in ours.values():
        if t.families.get("income"):
            for k, v in t.legs.items():
                if v > 0:
                    inc_ours[(t.chain, _year(t.ts), k.upper())] += v
    w("\n## Income by year\n")
    w("CoinLedger Staking/Mining Income vs our reward rows. Cardano and Solana rewards are booked per epoch on "
      "both sides (ours: Koios reward history / getInflationReward, dated when the ledger credits them).\n")
    w("| chain | year | asset | CoinLedger | ours |")
    w("|---|---|---|---:|---:|")
    for ik in sorted(set(inc_cl) | set(inc_ours)):
        a, b = inc_cl.get(ik, Decimal(0)), inc_ours.get(ik, Decimal(0))
        w(f"| {ik[0]} | {ik[1]} | {ik[2]} | {a:.6f} | {b:.6f} |".replace(".000000 ", " "))

    # ---- unmatched
    w("\n## Only in CoinLedger (on-chain)\n")
    reasons: Counter[str] = Counter()
    sample: dict[str, list[CLEvent]] = defaultdict(list)
    our_labels = {_lab(t.label) for t in ours.values() if t.label}
    for e in onchain:
        if e.match:
            continue
        if e.chain not in covered:
            why = f"chain without an adapter ({e.chain})"
        elif not any(e.legs.values()):
            why = "fee-only or cost-basis adjustment event (no asset movement)"
        elif e.type == "Native Stake" and all(v < 0 for v in e.legs.values() if v):
            why = "CoinLedger re-stake entry paired with each reward (bookkeeping only, no on-chain counterpart)"
        elif e.type in ("Staking Income", "Native Stake") and e.chain == "Ethereum" \
                and not e.txid.startswith("0x"):
            why = ("expected: CoinLedger accrual of Ethereum validator rewards before they were distributed. Rewards "
                   "only started paying out once the withdrawal address was set, so ours books income at each "
                   "sweep (CoinLedger also counts those sweeps, so these accruals are counted twice there)")
        elif e.type in ("Staking Income", "Native Stake"):
            why = f"{e.chain} stake reward with no matching per-epoch reward row in ours"
        elif e.type == "Mining Income" and e.chain in ("XCH", "Chia") or e.account == "XCH" and e.type == "Mining Income":
            why = ("CoinLedger hand-entered Chia mining income (yearly lumps of pool payouts and some farmer rewards); "
                   "ours has each payout, compare the yearly totals under Income by year")
        elif e.type == "Manual Position":
            why = ("CoinLedger manual position (hand-entered balance fix with a USD cost basis; no on-chain "
                   "counterpart, ours has the real inflows instead)")
        elif e.type == "Bridge" and len([v for v in e.legs.values() if v]) == 1 and all(v < 0 for v in e.legs.values() if v):
            why = ("CoinLedger bridge entry recording only the amount the bridge kept (sent minus received); "
                   "ours has both the outgoing and the arriving legs")
        elif _lab(e.account) not in our_labels:
            why = f"CoinLedger account '{e.account}' (name not matching an addresses.csv label)"
        elif not e.txid:
            why = "no tx id and no amount/time match"
        else:
            why = "tx id not in our data"
        reasons[why] += 1
        sample[why].append(e)
    for why, n in reasons.most_common():
        w(f"- **{why}**: {n}")
        for e in sorted(sample[why], key=lambda e: -sum(abs(v) for v in e.legs.values()))[:3]:
            w(f"  - {_d(e.ts)} {e.type}: {_legs(e.legs)} `{(e.txid or e.iid)[:16]}…`")

    w("\n## Only in ours (on-chain, inside CoinLedger's date range)\n")
    for ch, ts in sorted(ours_only.items(), key=lambda kv: -len(kv[1])):
        w(f"- **{ch}**: {len(ts)}")
        for t in sorted(ts, key=lambda t: -t.usd)[:4]:
            w(f"  - {_d(t.ts)} {', '.join(t.classes)}: {_legs(t.legs)} `{t.tx_hash[:16]}…`")
    md.write_text("\n".join(L) + "\n")

    with diff_csv.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["kind", "cl_internal_id", "date_utc", "platform", "account", "cl_type", "cl_tx_id", "cl_legs",
                     "cl_fees", "match", "our_chain", "our_tx_hash", "our_wallet", "our_classes", "our_legs", "review"])
        for e in cl:
            if e.chain in EXCHANGES:
                continue
            ot = ours[e.our_key] if e.our_key else None
            fam = ot.families.most_common(1)[0][0] if ot else ""
            flag = "" if not ot else "" if CL_FAMILY.get(e.type, "other") == fam or (e.type, fam) in EXPECTED else "class"
            wr.writerow(["event", e.iid, _d(e.ts), e.platform, e.account, e.type, e.txid, _legs(e.legs), _legs(e.fees),
                         e.match or "cl_only", ot.chain if ot else "", ot.tx_hash if ot else "", ot.wallet if ot else "",
                         ";".join(ot.classes) if ot else "", _legs(ot.legs) if ot else "", flag])
        for p, d, k, a, b in days:
            ok = _close(a, b) or abs(a - b) < Decimal("1e-6")
            wr.writerow(["exchange_day", "", d, p, "", "", "", f"{k.upper()} {a.normalize():f}", "", "",
                         p, "", "", "", f"{k.upper()} {b.normalize():f}", "" if ok else "amount"])
    return {"onchain_events": len(onchain), "tx_id": sum(1 for e in onchain if e.match == "tx_id"),
            "amount_time": sum(1 for e in onchain if e.match == "amount_time"),
            "cl_only": sum(1 for e in onchain if not e.match), "ours_only": sum(len(v) for v in ours_only.values()),
            "class_review": sum(review.values()), "amount_diffs": len(diffs),
            "exchange_day_pairs": len(days), "exchange_day_diffs": len(ex_bad)}


def _ignorable(t: OurTx) -> bool:
    fams = set(t.families)
    return not t.legs or fams <= {"spam", "other"} and all(
        c.split("/")[1] in ("fee_payer_only", "stake_management", "account_maintenance", "approval", "consolidation",
                            "governance_vote", "consensus_keyreg") or c.split("/")[1].startswith(("spam", "contract_call"))
        for c in t.classes)
