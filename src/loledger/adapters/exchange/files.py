"""File-based exchange ledgers (MEXC, Gemini) -> the same Movement/TxContext shape as the Crypto.com reader.

Each tx carries meta["journal_type"]:
  TRADING                one order/fill group (legs: bought +, sold -, fee legs source="fee")
  ONCHAIN_DEPOSIT / ONCHAIN_WITHDRAWAL  with meta txid (+ txid_index) and network for exact linking
  FIAT_DEPOSIT / FIAT_WITHDRAWAL, INTERNAL_MOVE (spot <-> futures), FUTURES_PNL, ADMIN
"""

from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ...model import AssetKind, Movement, TxContext

log = logging.getLogger(__name__)

FIAT = {"USD", "EUR", "GBP"}
# MEXC deposit/withdrawal currencies carry the network: "AVAX-CCHAIN", "USDT-SOL", "USDT-ERC20" ...
NETWORK_SUFFIXES = {"CCHAIN", "AVAXC", "SOL", "ERC20", "TRC20", "BEP20", "BSC", "ARB", "ARBITRUM", "MATIC", "POLYGON",
                    "OP", "OPTIMISM", "BASE", "ETH", "ADA", "SPL", "KRC20"}


def split_network(currency: str) -> tuple[str, str]:
    asset, sep, net = currency.partition("-")
    if sep and net.upper() in NETWORK_SUFFIXES:
        return asset, net
    return currency, ""


@dataclass
class Ledger:
    moves: list[Movement]
    txs: dict[str, TxContext]
    checks: list[str]  # human-readable self-check results


def _ts(s: str, fmt: str, tz_hours: float = 0) -> int:
    dt = datetime.strptime(s.strip(), fmt).replace(tzinfo=UTC) - timedelta(hours=tz_hours)
    return int(dt.timestamp())


class _Builder:
    def __init__(self, chain: str) -> None:
        self.chain = chain
        self.moves: list[Movement] = []
        self.txs: dict[str, TxContext] = {}
        self.n = 0

    def leg(self, wallet: str, h: str, ts: int, asset: str, amount: Decimal, source: str, cp: str,
            meta: dict[str, Any]) -> None:
        if amount == 0:
            return
        self.n += 1
        self.moves.append(Movement(chain=self.chain, tx_hash=h, leg_id=f"{source}:{self.n}", block=0, timestamp=ts,
                                   wallet=wallet, asset_id=asset, asset_kind=AssetKind.EXCHANGE, symbol=asset,
                                   decimals=8, amount=amount, counterparty=cp, source=source))
        t = self.txs.get(h)
        if t is None:
            self.txs[h] = TxContext(chain=self.chain, tx_hash=h, block=0, timestamp=ts, tx_from=wallet, tx_to=cp,
                                    method_id="", function_name=str(meta.get("journal_type", "")).lower(),
                                    is_error=False, gas_fee=Decimal(0), value=Decimal(0), meta=dict(meta))
        else:
            t.timestamp = min(t.timestamp, ts)

    def ledger(self, checks: list[str]) -> Ledger:
        self.moves.sort(key=lambda m: (m.timestamp, m.tx_hash, m.leg_id))
        return Ledger(self.moves, self.txs, checks)


def merge_rows(per_file: list[list[dict[str, str]]], key: Any = None) -> tuple[list[dict[str, str]], int]:
    """Rows of several export files with overlapping date ranges, each record once. Identical rows inside one file
    are separate records (two equal fills); across files a record counts as many times as the file with the most
    copies has it. key(row) -> hashable; default: every non-empty column. Returns (rows, duplicates dropped)."""
    def full(r: dict[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((k, (v or "").strip()) for k, v in r.items() if k and (v or "").strip()))

    kf = key or full
    best: dict[Any, tuple[int, dict[str, str]]] = {}
    for rows in per_file:
        here: dict[Any, int] = defaultdict(int)
        for r in rows:
            k = kf(r)
            here[k] += 1
            if here[k] > best.get(k, (0, r))[0]:
                best[k] = (here[k], r)
    out = [r for n, r in best.values() for _ in range(n)]
    return out, sum(len(rows) for rows in per_file) - len(out)


def _hex_lower(t: str) -> str:
    """Lowercase hex hashes only; base58 signatures (Solana) are case-sensitive."""
    body = t[2:] if t.lower().startswith("0x") else t
    return t.lower() if body and all(c in "0123456789abcdefABCDEF" for c in body) else t


def _split_txid(txid: str) -> tuple[str, str]:
    txid = (txid or "").strip()
    for sep in ("/", ":"):
        if sep in txid and not txid.startswith("0x" + sep):
            h, _, idx = txid.partition(sep)
            return _hex_lower(h), idx
    return _hex_lower(txid), ""


# ---- MEXC ----------------------------------------------------------------------------------------

def parse_mexc(folder: Path, tz_hours: float = 0, chain: str = "MEXC", web_tz_hours: float = -4) -> Ledger:
    """MEXC 'export all' CSVs: *spot.csv, *deposit.csv, *withdraw.csv, *transfer.csv, *contract.csv (UTC+8),
    plus website history downloads (trade / deposit / withdrawal history, UTC-4) for what 'export all' lacks."""
    b = _Builder(chain)
    spot_w, fut_w = "mexc:spot", "mexc:futures"
    checks: list[str] = []

    def rows(pattern: str) -> list[dict[str, str]]:
        files = [list(csv.DictReader(f.open(encoding="utf-8-sig"))) for f in sorted(folder.glob(f"*{pattern}.csv"))]
        out, dupes = merge_rows(files)
        if dupes:
            checks.append(f"MEXC {pattern}: {dupes} row(s) repeated across export files (overlapping ranges); counted once")
        return out

    fmt = "%Y-%m-%d %H:%M:%S"
    for r in rows("spot"):
        base, _, quote = r["symbol"].partition("-")
        ts = _ts(r["create_time"], fmt, tz_hours)
        qty, price = Decimal(r["quantity"]), Decimal(r["price"])
        sign = 1 if r["side"].upper() == "BUY" else -1
        h = f"mexc:trade:{r['symbol']}:{r['side']}:{r['create_time']}"
        meta: dict[str, Any] = {"journal_type": "TRADING", "instrument": r["symbol"]}
        b.leg(spot_w, h, ts, base, sign * qty, "trade", "mexc", meta)
        b.leg(spot_w, h, ts, quote, -sign * qty * price, "trade", "mexc", meta)
        b.leg(spot_w, h, ts, r["fee_currency"], -Decimal(r["fee"] or 0), "fee", "mexc", meta)
        b.txs[h].meta["fills"] = int(b.txs[h].meta.get("fills", 0)) + 1
    skipped = 0
    for r in rows("deposit"):
        if r["status"] != "SUCCESS":
            skipped += 1
            continue
        txh, idx = _split_txid(r["txid"])
        h = f"mexc:deposit:{r['txid']}:{r['create_time']}"
        asset, net = split_network(r["currency"])
        b.leg(spot_w, h, _ts(r["create_time"], fmt, tz_hours), asset, Decimal(r["actual_amount"] or r["amount"]),
              "onchain_deposit", "mexc", {"journal_type": "ONCHAIN_DEPOSIT", "txid": txh, "txid_index": idx,
                                          "network": net or None})
    if skipped:
        checks.append(f"MEXC: {skipped} deposit(s) with status != SUCCESS (e.g. SMALL: below minimum, not credited) left out")
    for r in rows("withdraw"):
        if r["state"] != "SUCCESS":
            continue
        txh, idx = _split_txid(r["txid"])
        h = f"mexc:withdrawal:{r['txid']}:{r['create_time']}"
        ts = _ts(r["create_time"], fmt, tz_hours)
        asset, net = split_network(r["currency"])
        meta = {"journal_type": "ONCHAIN_WITHDRAWAL", "txid": txh, "txid_index": idx, "address": r.get("address"),
                "fee": r["fee"], "net_amount": r["actual_amount"], "network": net or None}
        b.leg(spot_w, h, ts, asset, -Decimal(r["actual_amount"]), "onchain_withdrawal", "mexc", meta)
        b.leg(spot_w, h, ts, asset, -Decimal(r["fee"] or 0), "fee", "mexc", meta)
    for r in rows("transfer"):
        if r["state"] != "SUCCESS":
            continue
        acct = {"MAIN": spot_w, "SWAP": fut_w}
        src, dst = acct.get(r["from_sys"]), acct.get(r["to_sys"])
        if not src or not dst:
            continue
        h = f"mexc:move:{r['id']}"
        ts = _ts(r["create_time"], fmt, tz_hours)
        amt = Decimal(r["amount"])
        meta = {"journal_type": "INTERNAL_MOVE", "from": r["from_sys"], "to": r["to_sys"]}
        b.leg(src, h, ts, r["currency"], -amt, "internal_move", dst, meta)
        b.leg(dst, h, ts, r["currency"], amt, "internal_move", src, meta)
    for r in rows("contract"):
        profit, fee = Decimal(r.get("profit") or 0), Decimal(r.get("fee") or 0)
        cur = r.get("fee_currency") or "USDT"
        h = f"mexc:futures:{r['order_id']}"
        ts = _ts(r["create_time"], fmt, tz_hours)
        meta = {"journal_type": "FUTURES_PNL", "instrument": r["contract_name"], "side": r.get("side_en"),
                "deal_vol": str(Decimal(r.get("deal_vol") or 0).normalize()), "deal_avg_price": r.get("deal_avg_price"),
                "leverage": r.get("leverage"), "raw_time": r["create_time"]}
        b.leg(fut_w, h, ts, cur, profit, "futures_pnl", "mexc", meta)
        b.leg(fut_w, h, ts, cur, -fee, "fee", "mexc", meta)
    _mexc_web(folder, b, spot_w, fmt, web_tz_hours, checks)
    _mexc_futures_history(folder, b, fut_w, fmt, tz_hours, checks)
    fut: dict[str, Decimal] = defaultdict(Decimal)
    for m in b.moves:
        if m.wallet == fut_w:
            fut[m.asset_id] += m.amount
    checks.append("MEXC futures account balance implied by transfers + realized PnL − fees: " +
                  (", ".join(f"{v.normalize():f} {k}" for k, v in sorted(fut.items()) if v) or "0 (fully closed out)"))
    return b.ledger(checks)


def _mexc_futures_history(folder: Path, b: _Builder, fut_w: str, fmt: str, tz_hours: float, checks: list[str]) -> None:
    """Website 'Futures Order History' downloads (header 'Time (UTC+8)'): realized PnL and fee per finished order,
    for orders the 'export all' contract file does not list. Downloads overlap (different date ranges, a
    hand-kept running list), so identical orders are taken once per copy in the file that has the most of
    them. Only MEXC's own columns are read; formula columns and total rows added in a spreadsheet are ignored."""
    # the contract export lists fills (an order can be split, a second apart), so it cannot be matched order by
    # order: it is authoritative from its first row on, website orders fill in before that
    starts = [t.timestamp for t in b.txs.values() if t.meta.get("journal_type") == "FUTURES_PNL"]
    contract_start = min(starts) if starts else None
    with_contract = any(folder.glob("*contract.csv"))
    orders: dict[tuple[str, str, str, str], tuple[int, dict[str, str]]] = {}
    n_files = 0
    for f in sorted(folder.glob("*.csv")):
        with f.open(encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            if "Time (UTC+8)" not in (rd.fieldnames or []) or "Closing PNL" not in (rd.fieldnames or []):
                continue
            n_files += 1
            here: dict[tuple[str, ...], int] = defaultdict(int)
            for r in rd:
                if r.get("Status") != "FINISHED" or not r.get("Futures Trading Pair"):
                    continue
                k = (r["Futures Trading Pair"], r["Time (UTC+8)"], r["Direction"],
                     str(Decimal(r["Filled Quantity"]).normalize()))
                here[k] += 1
                if here[k] > orders.get(k, (0, r))[0]:
                    orders[k] = (here[k], r)
    n = 0
    for k, (copies, r) in sorted(orders.items()):
        cur = r["Fee-payment Crypto"] or "USDT"
        ts = _ts(r["Time (UTC+8)"], fmt, tz_hours)
        if with_contract and contract_start is not None and ts >= contract_start - 60:
            continue
        for c in range(copies):
            h = f"mexc:futures-web:{k[0]}:{k[1]}:{k[2]}:{k[3]}:{c}"
            meta = {"journal_type": "FUTURES_PNL", "instrument": k[0], "side": r["Direction"], "deal_vol": k[3],
                    "deal_avg_price": r["Average Filled Price"], "leverage": r["Leverage"], "source_file": "website"}
            b.leg(fut_w, h, ts, cur, Decimal(r["Closing PNL"] or 0), "futures_pnl", "mexc", meta)
            b.leg(fut_w, h, ts, cur, -Decimal(r["Trading Fee"] or 0), "fee", "mexc", meta)
            n += 1
    if n:
        checks.append(f"MEXC website futures order history ({n_files} files, overlaps removed): {n} finished orders "
                      "before the contract export starts")


def _mexc_web(folder: Path, b: _Builder, spot_w: str, fmt: str, tz_hours: float, checks: list[str]) -> None:
    """Website downloads (Trade History, Deposit History, Withdrawal History; UTC-4), recognised by header.
    'Export all' is authoritative: website trades are used only before its first spot fill, website
    deposits/withdrawals only when their TxID is not in it. Rows repeated across overlapping downloads, and
    rows pasted in from 'export all' (hyphenated pairs, UTC+8 times), are skipped."""
    spot_start = min((m.timestamp for m in b.moves if m.source == "trade"), default=None)
    known: set[str] = set()  # every TxID 'export all' lists, whatever its status (it is the final word)
    for pat in ("*deposit.csv", "*withdraw.csv"):
        for f in folder.glob(pat):
            with f.open(encoding="utf-8-sig") as fh:
                known |= {_split_txid(r["txid"])[0].lower() for r in csv.DictReader(fh) if r.get("txid")}
    trades: dict[tuple[str, ...], tuple[int, dict[str, str]]] = {}  # key -> (most copies in any one file, row)
    deps: dict[str, dict[str, str]] = {}
    wds: dict[str, dict[str, str]] = {}
    for f in sorted(folder.glob("*.csv")):
        with f.open(encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            cols = set(rd.fieldnames or [])
            here: dict[tuple[str, ...], int] = defaultdict(int)  # identical fills in one file are separate fills
            for r in rd:
                if {"Pairs", "Executed Amount", "Total"} <= cols and r.get("Pairs") and "_" in r["Pairs"]:
                    tk = (r["Pairs"], r["Time"], r["Side"], r["Executed Amount"], r["Total"], r["Fee"])
                    here[tk] += 1
                    if here[tk] > trades.get(tk, (0, r))[0]:
                        trades[tk] = (here[tk], r)
                elif {"Deposit Amount", "TxID"} <= cols and r.get("Status", "").startswith("Credited"):
                    deps.setdefault(_split_txid(r["TxID"])[0].lower(), r)
                elif {"Settlement Amount", "TxID"} <= cols and r.get("Status", "").startswith("Withdrawal Succ"):
                    wds.setdefault(_split_txid(r["TxID"])[0].lower(), r)
    n_t = n_d = n_w = 0
    for copies, r in trades.values():
        ts = _ts(r["Time"], fmt, tz_hours)
        if spot_start is not None and ts >= spot_start:
            continue
        base, _, quote = r["Pairs"].partition("_")
        sign = 1 if r["Side"].upper() == "BUY" else -1
        h = f"mexc:webtrade:{r['Pairs']}:{r['Side']}:{r['Time']}"
        meta: dict[str, Any] = {"journal_type": "TRADING", "instrument": f"{base}-{quote}", "source_file": "website"}
        for _ in range(copies):
            b.leg(spot_w, h, ts, base, sign * Decimal(r["Executed Amount"]), "trade", "mexc", meta)
            b.leg(spot_w, h, ts, quote, -sign * Decimal(r["Total"]), "trade", "mexc", meta)
            b.leg(spot_w, h, ts, quote, -Decimal(r["Fee"] or 0), "fee", "mexc", meta)  # website fee: quote asset
            b.txs[h].meta["fills"] = int(b.txs[h].meta.get("fills", 0)) + 1
            n_t += 1
    for k, r in deps.items():
        if k in known:
            continue
        txh, idx = _split_txid(r["TxID"])
        h = f"mexc:deposit:{r['TxID']}:{r['Time']}"
        b.leg(spot_w, h, _ts(r["Time"], fmt, tz_hours), r["Crypto"], Decimal(r["Deposit Amount"]), "onchain_deposit",
              "mexc", {"journal_type": "ONCHAIN_DEPOSIT", "txid": txh, "txid_index": idx, "network": r.get("Network")
                       or None, "source_file": "website"})
        n_d += 1
    for k, r in wds.items():
        if k in known:
            continue
        txh, idx = _split_txid(r["TxID"])
        h = f"mexc:withdrawal:{r['TxID']}:{r['Time']}"
        ts = _ts(r["Time"], fmt, tz_hours)
        meta = {"journal_type": "ONCHAIN_WITHDRAWAL", "txid": txh, "txid_index": idx,
                "address": r.get("Withdrawal Address"), "fee": r["Trading Fee"], "net_amount": r["Settlement Amount"],
                "network": r.get("Network") or None, "source_file": "website"}
        b.leg(spot_w, h, ts, r["Crypto"], -Decimal(r["Settlement Amount"]), "onchain_withdrawal", "mexc", meta)
        b.leg(spot_w, h, ts, r["Crypto"], -Decimal(r["Trading Fee"] or 0), "fee", "mexc", meta)
        n_w += 1
    if n_t or n_d or n_w:
        checks.append(f"MEXC website downloads (UTC{tz_hours:+g}) fill gaps in 'export all': {n_t} trade fills before "
                      f"its first spot fill, {n_d} deposits and {n_w} withdrawals it does not list")


# ---- Gemini --------------------------------------------------------------------------------------

_NUM = re.compile(r"[^0-9.\-]")


def _gemini_num(s: str) -> Decimal:
    s = (s or "").strip()
    if not s:
        return Decimal(0)
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = Decimal(_NUM.sub("", s.split(" ")[0] if not s.startswith(("$", "(")) else s))
    except InvalidOperation:
        return Decimal(0)
    return -abs(v) if neg else v


def _display_tol(cell: str) -> Decimal:
    """Gemini rounds displayed amounts; allow two units of the last decimal the balance cell shows."""
    num = cell.replace("$", "").replace(",", "").strip("() ").split(" ")[0]
    places = len(num.split(".")[1]) if "." in num else 0
    return Decimal(2) * Decimal(10) ** -max(places, 2 if cell.strip().startswith(("$", "($")) else places)


def _gemini_key(r: dict[str, str]) -> tuple[Any, ...]:
    """A Gemini record, whatever the export: balance columns are left out (a newer export adds one per newly
    traded asset), as are empty amount/fee columns."""
    fixed = tuple((r.get(c) or "").strip() for c in ("Date", "Time (UTC)", "Type", "Symbol", "Specification",
                                                     "Trade ID", "Order ID", "Tx Hash"))
    amounts = tuple(sorted((k, v.strip()) for k, v in r.items()
                           if k and (" Amount " in k or k.startswith("Fee (")) and (v or "").strip()))
    return fixed + amounts


def parse_gemini(path: Path, chain: str = "Gemini") -> Ledger:
    """Gemini 'transaction history' CSV(s): a file, or a folder of them (overlapping exports are merged, each
    record once). Self-check: running sums must equal the per-row balance columns."""
    b = _Builder(chain)
    w = "gemini:exchange"
    files = [path] if path.is_file() else sorted(f for f in path.glob("*.csv")
                                                 if "Time (UTC)" in f.open(encoding="utf-8-sig").readline())
    per_file = [list(csv.DictReader(f.open(encoding="utf-8-sig"))) for f in files]
    body, dupes = merge_rows([[r for r in rows if r["Type"].strip()] for rows in per_file], _gemini_key)
    # the totals row of the export that runs latest (the last row with no Type)
    tots = [(rows[-2]["Date"] if len(rows) > 1 else "", rows[-1]) for rows in per_file if rows and not rows[-1]["Type"].strip()]
    rows = sorted(body, key=lambda r: (r["Date"], r["Time (UTC)"])) + ([max(tots, key=lambda t: t[0])[1]] if tots else [])
    cols = sorted({c for rs in per_file for r in rs[:1] for c in r})
    assets = sorted({c.split(" Amount ")[0] for c in cols if " Amount " in c})
    running: dict[str, Decimal] = defaultdict(Decimal)
    bad_rows = 0
    for i, r in enumerate(rows):
        typ = r["Type"].strip()
        if not typ:
            continue  # trailing totals row
        ts = _ts(f"{r['Date']} {r['Time (UTC)'][:8]}", "%Y-%m-%d %H:%M:%S")
        spec = r["Specification"]
        amounts = {a: _gemini_num(r.get(f"{a} Amount {a}", "")) for a in assets}
        fees = {a: _gemini_num(r.get(f"Fee ({a}) {a}", "")) for a in assets}
        if typ in ("Buy", "Sell"):
            h = f"gemini:order:{r['Order ID'] or r['Trade ID']}"
            meta: dict[str, Any] = {"journal_type": "TRADING", "instrument": r["Symbol"]}
        elif spec.startswith("Administrative"):
            h, meta = f"gemini:admin:{i}", {"journal_type": "ADMIN", "spec": spec}
        elif typ == "Credit" and spec == "Deposit" and not (r.get("Tx Hash") or "").strip():
            # bare "Deposit" with no tx hash/address/fee: an internal Gemini credit (likely staking/earn reward)
            h, meta = f"gemini:credit:{i}", {"journal_type": "EXCHANGE_CREDIT", "spec": spec}
        else:
            fiat = r["Symbol"] in FIAT
            dep = typ == "Credit"
            jt = ("FIAT_" if fiat else "ONCHAIN_") + ("DEPOSIT" if dep else "WITHDRAWAL")
            out = r.get("Deposit Tx Output" if dep else "Withdrawal Tx Output", "")
            txh = _hex_lower((r.get("Tx Hash") or "").strip())
            meta = {"journal_type": jt, "txid": txh, "txid_index": out.strip(), "spec": spec,
                    "address": r.get("Withdrawal Destination") or r.get("Deposit Destination")}
            h = f"gemini:{jt.lower()}:{i}"
        for a in assets:
            src = {"TRADING": "trade", "ADMIN": "admin"}.get(meta["journal_type"], meta["journal_type"].lower())
            b.leg(w, h, ts, a, amounts[a], src, "gemini", meta)
            b.leg(w, h, ts, a, -abs(fees[a]), "fee", "gemini", meta)
            running[a] += amounts[a] - abs(fees[a])
        if meta["journal_type"] == "TRADING":
            b.txs[h].meta["fills"] = int(b.txs[h].meta.get("fills", 0)) + 1 if h in b.txs else 1
        for a in assets:
            bal = r.get(f"{a} Balance {a}", "").strip()
            if bal and abs(_gemini_num(bal) - running[a]) > _display_tol(bal):
                bad_rows += 1
                if bad_rows <= 3:
                    log.warning("  Gemini row %d (%s %s): running %s %s != balance column %s", i + 2, r["Date"], typ,
                                running[a], a, bal)
                running[a] = _gemini_num(bal)  # resync so one bad row is reported once
    totals = next((r for r in reversed(rows) if not r["Type"].strip()), None)
    diffs = []
    if totals:
        for a in assets:
            cell = totals.get(f"{a} Balance {a}", "").strip()
            if cell and abs(_gemini_num(cell) - running[a]) > Decimal("0.000001") * max(Decimal(1), abs(running[a])):
                diffs.append(f"{a}: ours {running[a].normalize():f} vs totals row {cell}")
    final = {a: v for a, v in running.items() if abs(v) > Decimal("1e-9")}
    return b.ledger(([f"Gemini: {dupes} row(s) repeated across {len(files)} export files; counted once"] if dupes else []) + [
        f"Gemini: {len(rows)} rows; per-row running balances differ beyond display rounding on {bad_rows} row/asset(s) "
        f"(cents of USD from Gemini's rounding)",
        "Gemini final balances vs the export's totals row: " + ("all match" if totals and not diffs else
                                                                 ("; ".join(diffs) or "no totals row")),
        "Gemini final: " + ", ".join(f"{v.normalize():f} {k}" for k, v in sorted(final.items())),
    ])


# ---- OKX -----------------------------------------------------------------------------------------

def _okx_csvs(folder: Path) -> list[tuple[str, list[dict[str, str]]]]:
    """OKX exports are zips (or extracted CSVs) whose first line is 'UID:…,Account Type:…,Time Zone:UTC0'."""
    import io
    import zipfile

    out: list[tuple[str, list[dict[str, str]]]] = []
    blobs: list[tuple[str, str]] = []
    for f in sorted(folder.glob("*")):
        if f.suffix == ".zip":
            with zipfile.ZipFile(f) as z:
                blobs += [(n, z.read(n).decode("utf-8-sig")) for n in z.namelist() if n.endswith(".csv")]
        elif f.suffix == ".csv":
            blobs.append((f.name, f.read_text(encoding="utf-8-sig")))
    for name, text in blobs:
        lines = text.splitlines()
        if lines and lines[0].startswith("UID:"):
            if "UTC0" not in lines[0]:
                log.warning("OKX %s: unexpected time zone header %r", name, lines[0])
            lines = lines[1:]
        out.append((name, list(csv.DictReader(io.StringIO("\n".join(lines))))))
    return out


def parse_okx(folder: Path, chain: str = "OKX", api: tuple[list[dict[str, str]], list[dict[str, str]]] | None = None,
              ) -> Ledger:
    """OKX export files, plus rows from the API (adapters/exchange/okx.py) in the same layout when given. Rows are
    merged by OKX bill id; API rows win (they carry the withdrawal fee and the on-chain tx hash)."""
    b = _Builder(chain)
    w = "okx:account"
    fmt = "%Y-%m-%d %H:%M:%S"
    checks: list[str] = []
    # newer exports overlap older ones: merge each kind across files, one row per OKX bill id
    trading: dict[str, dict[str, str]] = {}
    funding: dict[str, dict[str, str]] = {}
    dupes = 0
    for name, rows in _okx_csvs(folder) if folder.exists() else []:
        kind = trading if rows and "Order id" in rows[0] else funding if rows and "Before Balance" in rows[0] else None
        if kind is None:
            continue
        for r in rows:
            if r["id"] in kind:
                dupes += 1
            kind[r["id"]] = r
    if dupes:
        checks.append(f"OKX: {dupes} row(s) appear in more than one export (overlapping date ranges); counted once")
    if api is not None:
        n_exp = len(trading) + len(funding)
        both = sum(1 for r in api[0] if r["id"] in trading) + sum(1 for r in api[1] if r["id"] in funding)
        trading.update({r["id"]: r for r in api[0]})
        funding.update({r["id"]: r for r in api[1]})
        checks.append(f"OKX: {n_exp} export row(s), {len(api[0]) + len(api[1])} API row(s), {both} in both")
    for rows in (list(trading.values()), list(funding.values())):
        if rows and "Order id" in rows[0]:  # trading bill history: one row per currency per fill
            last_bal: dict[str, Decimal] = {}
            for r in sorted(rows, key=lambda r: (r["Time"], r["id"])):
                h = f"okx:order:{r['Order id']}"
                ts = _ts(r["Time"], fmt)
                unit = r["Balance Unit"]
                change, fee = Decimal(r["Balance Change"]), Decimal(r["Fee"] or 0)
                meta: dict[str, Any] = {"journal_type": "TRADING", "instrument": r["Symbol"]}
                b.leg(w, h, ts, unit, change - fee, "trade", "okx", meta)
                b.leg(w, h, ts, r["Fee Unit"] or unit, fee, "fee", "okx", meta)
                last_bal[unit] = Decimal(r["Balance"])
            checks.append("OKX trading account balances after the last fill: " +
                          ", ".join(f"{v.normalize():f} {k}" for k, v in sorted(last_bal.items())))
        elif rows and "Before Balance" in rows[0]:  # funding history
            for r in rows:
                typ = r["Type"]
                if typ not in ("Deposit", "Withdrawal"):
                    checks.append(f"OKX funding row type {typ!r} not modelled")
                    continue
                jt = "ONCHAIN_DEPOSIT" if typ == "Deposit" else "ONCHAIN_WITHDRAWAL"
                h = f"okx:{jt.lower()}:{r['id']}"
                txh, idx = _split_txid(r.get("_txid", ""))
                meta = {"journal_type": jt, "txid": txh, "txid_index": idx,
                                        "address": r.get("_address") or None, "network": r.get("_network") or None}
                if r.get("_net"):  # API withdrawal: what left on chain + OKX's fee
                    meta.update(fee=r["_fee"], net_amount=r["_net"])
                    b.leg(w, h, _ts(r["Time"], fmt), r["Symbol"], -Decimal(r["_net"]), jt.lower(), "okx", meta)
                    b.leg(w, h, _ts(r["Time"], fmt), r["Symbol"], -Decimal(r["_fee"]), "fee", "okx", meta)
                else:  # export row: the gross amount (fee included), no tx hash
                    meta["gross"] = True
                    b.leg(w, h, _ts(r["Time"], fmt), r["Symbol"], Decimal(r["Amount"]), jt.lower(), "okx", meta)
    return b.ledger(checks)


# ---- Binance.US ----------------------------------------------------------------------------------

def parse_binanceus(folder: Path, chain: str = "Binance.US") -> Ledger:
    """Binance.US 'Transaction Tax Report' CSVs. Transaction_Id is Binance-internal (no on-chain hash)."""
    b = _Builder(chain)
    w = "binanceus:account"
    seen: set[str] = set()
    for f in sorted(folder.glob("*.csv")):
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            key = f"{r['Transaction_Id']}:{r['Time']}:{r['Operation']}"
            if key in seen:
                continue  # overlapping report periods
            seen.add(key)
            ts = _ts(r["Time"], "%Y-%m-%d %H:%M:%S")
            cat, op = r["Category"], r["Operation"]

            def dec(k: str) -> Decimal:
                return Decimal(r.get(k) or 0)

            if cat == "Spot Trading":
                h = f"binanceus:order:{r['Order_Id']}"
                meta: dict[str, Any] = {"journal_type": "TRADING", "instrument": f"{r['Base_Asset']}{r['Quote_Asset']}"}
                sign = 1 if op == "Buy" else -1
                b.leg(w, h, ts, r["Base_Asset"], sign * dec("Realized_Amount_For_Base_Asset"), "trade", "binance.us", meta)
                b.leg(w, h, ts, r["Quote_Asset"], -sign * dec("Realized_Amount_For_Quote_Asset"), "trade", "binance.us", meta)
                b.leg(w, h, ts, r["Fee_Asset"], -dec("Realized_Amount_For_Fee_Asset"), "fee", "binance.us", meta)
            elif cat in ("Deposit", "Withdrawal"):
                dep = cat == "Deposit"
                fiat = r["Primary_Asset"] in FIAT
                jt = ("FIAT_" if fiat else "ONCHAIN_") + ("DEPOSIT" if dep else "WITHDRAWAL")
                h = f"binanceus:{jt.lower()}:{r['Transaction_Id']}"
                amt = dec("Realized_Amount_For_Primary_Asset")
                meta = {"journal_type": jt, "txid": "", "binance_tx_id": r["Transaction_Id"], "spec": op}
                b.leg(w, h, ts, r["Primary_Asset"], amt if dep else -amt, jt.lower(), "binance.us", meta)
                if r.get("Fee_Asset") and dec("Realized_Amount_For_Fee_Asset"):
                    b.leg(w, h, ts, r["Fee_Asset"], -dec("Realized_Amount_For_Fee_Asset"), "fee", "binance.us", meta)
    return b.ledger([])


# ---- Coinbase (hand-made deposits list) ----------------------------------------------------------

def parse_coinbase_manual(path: Path, chain: str = "Coinbase") -> Ledger:
    """User-made CSV: Date,Type,From Label,Amount,Asset (e.g. 'Sep 13, 2026 07:46 PM,Deposit,ETH (Tangem),202,USDC').

    Times are as shown in Coinbase (local time, zone unknown), so linking allows a wide window.
    """
    b = _Builder(chain)
    w = "coinbase:account"
    raw = list(csv.reader(path.open(encoding="utf-8-sig")))
    header, body = raw[0], raw[1:]
    for i, cells in enumerate(c for c in body if any(x.strip() for x in c)):
        if len(cells) == len(header) + 1:  # unquoted "Sep 13, 2026 07:46 PM" split at its comma
            cells = [f"{cells[0]},{cells[1]}", *cells[2:]]
        r = dict(zip(header, cells, strict=False))
        ts = _ts(r["Date"], "%b %d, %Y %I:%M %p")
        typ = r["Type"].strip().lower()
        dep = typ.startswith("deposit")
        jt = "ONCHAIN_DEPOSIT" if dep else "ONCHAIN_WITHDRAWAL"
        amt = Decimal(r["Amount"].replace(",", ""))
        b.leg(w, f"coinbase:{typ}:{i}", ts, r["Asset"].strip(), amt if dep else -amt, jt.lower(), "coinbase",
              {"journal_type": jt, "txid": "", "wallet_label": r.get("From Label") or r.get("To Label") or "",
               "time_zone_unknown": True})
    return b.ledger([])
