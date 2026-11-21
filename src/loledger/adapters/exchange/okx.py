"""OKX account history through the v5 REST API (read-only key: OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE).

A key only works on the OKX site it was created on: us.okx.com for US accounts (the default here), www.okx.com
global, eea.okx.com EEA. Set OKX_API_BASE to change it.

What is read:
- trading-account bills (/account/bills-archive, about the last 3 months): one row per currency per fill;
- funding-account bills (/asset/bills-history): deposits, withdrawals, and transfers between OKX's own funding
  and trading accounts (internal to the one OKX account here, so left out);
- deposit / withdrawal history: the on-chain tx hash, address, network and withdrawal fee for each funding bill.
The API keeps only recent months, so every bill ever seen is accumulated in the store (by bill id) and history
stays complete across syncs. Bill ids are the same ids OKX's export files use, so API rows and export rows
merge in files.parse_okx (rows from the API win: they carry tx hashes and fees).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from ...config import ConfigError
from ...http import HttpClient, TransientError
from ...store import Store

log = logging.getLogger(__name__)
SOURCE = "okx-api"
DEFAULT_BASE = "https://us.okx.com"
# funding bill types: 1 deposit, 2 withdrawal; 130/131 = to/from the trading account (internal)
FUNDING_TYPES = {"1": "Deposit", "2": "Withdrawal"}
INTERNAL_FUNDING_TYPES = {"130", "131"}


def has_keys() -> bool:
    return all(os.environ.get(k) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"))


def sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    return base64.b64encode(hmac.new(secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).digest()).decode()


def _check(body: Any) -> None:
    code = str((body or {}).get("code", ""))
    if code in ("50011", "50061"):  # rate limit
        raise TransientError(f"OKX rate limit ({code})")
    if code != "0":
        raise ValueError(f"OKX API error {code}: {(body or {}).get('msg')}")


def utc(ms: str | int) -> str:
    """OKX millisecond timestamp -> the export files' 'YYYY-mm-dd HH:MM:SS' (UTC)."""
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


class OkxClient:
    def __init__(self, store: Store, http: HttpClient | None = None, base: str | None = None) -> None:
        if not has_keys():
            raise ConfigError("OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE not set (.env)")
        self.key, self.secret = os.environ["OKX_API_KEY"], os.environ["OKX_API_SECRET"]
        self.passphrase = os.environ["OKX_API_PASSPHRASE"]
        self.base = (base or os.environ.get("OKX_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.store = store
        self.http = http or HttpClient(4)  # private endpoints: 5-10 requests per 2 s

    def get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        full = path + (f"?{urlencode(q)}" if q else "")
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        self.http.client.headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign(self.secret, ts, "GET", full),
                                         "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase})
        body = self.http.request_json("GET", self.base + full, check=_check)
        return list(body.get("data") or [])

    def pages(self, path: str, cursor: str, params: dict[str, Any] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first; page backwards with after=<cursor field of the last row>."""
        out: list[dict[str, Any]] = []
        after = ""
        while True:
            rows = self.get(path, {**(params or {}), "limit": limit, "after": after})
            out += rows
            if len(rows) < limit:
                return out
            after = str(rows[-1][cursor])

    def _accumulate(self, endpoint: str, rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        """Merge freshly fetched rows into everything seen before (the API forgets old months)."""
        seen = {str(r[key]): r for r in (self.store.get_raw(SOURCE, endpoint, {}) or [])}
        new = sum(1 for r in rows if str(r[key]) not in seen)
        seen.update({str(r[key]): r for r in rows})
        merged = sorted(seen.values(), key=lambda r: (str(r.get("ts") or r.get("fillTime") or ""), str(r[key])))
        self.store.put_raw(SOURCE, endpoint, {}, merged)
        log.info("  okx %s: %d fetched, %d new, %d kept", endpoint, len(rows), new, len(merged))
        return merged

    def fetch(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "trading": self._accumulate("account_bills", self.pages("/api/v5/account/bills-archive", "billId"), "billId"),
            "funding": self._accumulate("asset_bills", self.pages("/api/v5/asset/bills-history", "ts"), "billId"),
            "deposits": self._accumulate("deposits", self.pages("/api/v5/asset/deposit-history", "ts"), "depId"),
            "withdrawals": self._accumulate("withdrawals", self.pages("/api/v5/asset/withdrawal-history", "ts"), "wdId"),
        }

    def stored(self) -> dict[str, list[dict[str, Any]]]:
        """Everything earlier syncs fetched, without calling OKX."""
        return {k: list(self.store.get_raw(SOURCE, e, {}) or []) for k, e in
                (("trading", "account_bills"), ("funding", "asset_bills"), ("deposits", "deposits"),
                 ("withdrawals", "withdrawals"))}

    def balances(self) -> dict[str, Decimal]:
        """Trading + funding account balances per currency."""
        out: dict[str, Decimal] = defaultdict(Decimal)
        for acct in self.get("/api/v5/account/balance"):
            for d in acct.get("details") or []:
                out[d["ccy"]] += Decimal(d.get("cashBal") or "0")
        for d in self.get("/api/v5/asset/balances"):
            out[d["ccy"]] += Decimal(d.get("bal") or "0")
        return {k: v for k, v in out.items() if v}


def to_export_rows(api: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """API rows -> (trading rows, funding rows) in the export files' column layout, plus notes for rows not modelled.
    Funding rows carry _txid/_address/_network (and _net/_fee for withdrawals) from the deposit/withdrawal history."""
    notes: list[str] = []
    trading: list[dict[str, str]] = []
    other: dict[str, int] = {}
    for b in api.get("trading", []):
        if b.get("type") == "2":  # trade
            trading.append({"id": b["billId"], "Order id": b.get("ordId", ""), "Time": utc(b.get("fillTime") or b["ts"]),
                            "Symbol": b.get("instId", ""), "Balance Change": b["balChg"], "Fee": b.get("fee") or "0",
                            "Fee Unit": b["ccy"], "Balance Unit": b["ccy"], "Balance": b.get("bal", "")})
        elif b.get("type") != "1":  # 1 = transfer between OKX's own funding/trading accounts
            other[f"trading type {b.get('type')}"] = other.get(f"trading type {b.get('type')}", 0) + 1
    deps = list(api.get("deposits", []))
    wds = list(api.get("withdrawals", []))
    funding: list[dict[str, str]] = []
    for b in api.get("funding", []):
        typ = str(b.get("type"))
        if typ in INTERNAL_FUNDING_TYPES:
            continue
        if typ not in FUNDING_TYPES:
            other[f"funding type {typ} ({b.get('notes', '')})"] = other.get(f"funding type {typ} ({b.get('notes', '')})", 0) + 1
            continue
        row = {"id": b["billId"], "Time": utc(b["ts"]), "Type": FUNDING_TYPES[typ], "Amount": b["balChg"],
               "Symbol": b["ccy"], "Before Balance": ""}
        amt = abs(float(b["balChg"]))
        if typ == "1":
            d = _closest(deps, b, lambda x: abs(float(x["amt"]) - amt) < 1e-9)
            if d:
                row.update(_txid=d.get("txId", ""), _address=d.get("to", ""), _network=d.get("chain", ""))
        else:
            w = _closest(wds, b, lambda x: abs(float(x["amt"]) + float(x.get("fee") or 0) - amt) < 1e-9)
            if w:
                row.update(_txid=w.get("txId", ""), _address=w.get("to", ""), _network=w.get("chain", ""),
                           _net=w["amt"], _fee=w.get("fee") or "0")
        funding.append(row)
    notes += [f"OKX API: {n} {k} bill(s) not modelled" for k, n in sorted(other.items())]
    return trading, funding, notes


def _closest(cands: list[dict[str, Any]], bill: dict[str, Any], ok: Any) -> dict[str, Any] | None:
    """The deposit/withdrawal record for a funding bill: same currency and amount, nearest in time (≤ 1 h)."""
    ts = int(bill["ts"])
    best = [c for c in cands if c.get("ccy") == bill["ccy"] and ok(c) and abs(int(c["ts"]) - ts) <= 3_600_000]
    if not best:
        return None
    c = min(best, key=lambda c: abs(int(c["ts"]) - ts))
    cands.remove(c)
    return c
