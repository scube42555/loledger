"""Crypto.com Exchange (API v1) reader: signed read-only calls, cached day windows.

Verified against the live API (2026-11-18):
  * signature: HMAC-SHA256(secret, method + id + api_key + paramString + nonce), hex; paramString is
    the params' keys sorted, each followed by its value (lists/dicts flattened recursively)
  * wide time windows silently return incomplete results (a 30-day get-trades window returned fewer
    rows than one week inside it), so history is read one UTC day at a time; a day that hits the
    row limit is split in half until it does not
  * deposit/withdrawal `txid` is "<tx hash>/<log index or vout>"
Past days are immutable and cached; the last two days are re-read on every run.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ...config import ConfigError
from ...http import HttpClient, TransientError
from ...store import Store

log = logging.getLogger(__name__)

BASE = "https://api.crypto.com/exchange/v1/"
DAY_MS = 86_400_000
LIMIT = 100
SOURCE = "cryptocom"


def param_string(obj: dict[str, Any] | None, level: int = 0) -> str:
    if obj is None:
        return ""
    if level >= 3:
        return str(obj)
    out = []
    for k in sorted(obj):
        v = obj[k]
        out.append(k)
        if v is None:
            out.append("null")
        elif isinstance(v, list):
            out.extend(param_string(x, level + 1) if isinstance(x, dict) else str(x) for x in v)
        elif isinstance(v, dict):
            out.append(param_string(v, level + 1))
        else:
            out.append(str(v))
    return "".join(out)


def sign(secret: str, method: str, req_id: int, api_key: str, params: dict[str, Any], nonce: int) -> str:
    payload = f"{method}{req_id}{api_key}{param_string(params)}{nonce}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _check(body: Any) -> None:
    if isinstance(body, dict) and body.get("code") not in (0, None):
        msg = str(body.get("message") or body.get("code"))
        if "TOO_MANY" in msg.upper() or body.get("code") in (10006, 429):
            raise TransientError(msg)
        raise ConfigError(f"Crypto.com API error {body.get('code')}: {msg}")


@dataclass
class CryptoComBundle:
    journal: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    deposits: list[dict[str, Any]] = field(default_factory=list)
    withdrawals: list[dict[str, Any]] = field(default_factory=list)


class CryptoComClient:
    def __init__(self, store: Store, http: HttpClient | None = None) -> None:
        self.key = os.environ.get("CRYPTOCOM_API_KEY") or ""
        self.secret = os.environ.get("CRYPTOCOM_API_SECRET") or ""
        if not (self.key and self.secret):
            raise ConfigError("CRYPTOCOM_API_KEY / CRYPTOCOM_API_SECRET not set (.env)")
        self.store = store
        self.http = http or HttpClient(3)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time() * 1000)
        req_id = now % 1_000_000_000
        req = {"id": req_id, "method": method, "api_key": self.key, "params": params, "nonce": now,
               "sig": sign(self.secret, method, req_id, self.key, params, now)}
        body = self.http.request_json("POST", BASE + method, json_body=req, check=_check)
        return dict(body.get("result") or {})

    def _cached(self, method: str, params: dict[str, Any], *, final: bool) -> dict[str, Any]:
        c = self.store.get_raw(SOURCE, method, params)
        if c is not None:
            return dict(c)
        res = self.call(method, params)
        if final:
            self.store.put_raw(SOURCE, method, params, res)
        return res

    def _window(self, method: str, start: int, end: int, now: int) -> list[dict[str, Any]]:
        params = {"start_time": start, "end_time": end, "limit": LIMIT}
        data = list(self._cached(method, params, final=end < now - 2 * DAY_MS).get("data") or [])
        if len(data) >= LIMIT:
            if end - start > 1:
                mid = (start + end) // 2
                return self._window(method, start, mid, now) + self._window(method, mid, end, now)
            log.warning("  crypto.com %s: >=%d rows in one millisecond at %d; some may be missing", method, LIMIT, start)
        return data

    def daily(self, method: str, since_ms: int, key: str) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        out: dict[str, dict[str, Any]] = {}
        day = since_ms - since_ms % DAY_MS
        n = 0
        while day < now:
            for row in self._window(method, day, min(day + DAY_MS, now), now):
                out[str(row.get(key))] = row
            day += DAY_MS
            n += 1
            if n % 60 == 0:
                log.info("  crypto.com %s: through %s (%d rows)", method,
                         datetime.fromtimestamp(day / 1000, UTC).strftime("%Y-%m-%d"), len(out))
        return sorted(out.values(), key=lambda r: int(r.get("event_timestamp_ms") or r.get("create_time") or 0))

    def paged(self, method: str, list_key: str, since_ms: int) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            res = self.call(method, {"start_ts": since_ms, "end_ts": now, "page_size": 200, "page": page})
            got = list(res.get(list_key) or [])
            rows.extend(got)
            if len(got) < 200:
                return rows
            page += 1

    def fetch(self, since: str) -> CryptoComBundle:
        since_ms = int(datetime.fromisoformat(since).replace(tzinfo=UTC).timestamp() * 1000)
        return CryptoComBundle(
            deposits=self.paged("private/get-deposit-history", "deposit_list", since_ms),
            withdrawals=self.paged("private/get-withdrawal-history", "withdrawal_list", since_ms),
            journal=self.daily("private/get-transactions", since_ms, "journal_id"),
            trades=self.daily("private/get-trades", since_ms, "trade_id"),
        )


# ---- normalize -----------------------------------------------------------------------------------

def _split_txid(txid: str) -> tuple[str, str]:
    h, _, idx = (txid or "").partition("/")
    return h.lower(), idx


def normalize(bundle: CryptoComBundle, chain_name: str = "Crypto.com",
              wallet: str = "cryptocom:exchange") -> tuple[list[Any], dict[str, Any]]:
    """Journal rows -> Movements (the journal reconciles to the live balance exactly).

    Trades are grouped per order (one tx per order: all fills + fees). On-chain deposits and
    withdrawals get the tx hash / log index / network from the deposit & withdrawal history.
    """
    from decimal import Decimal

    from ...model import AssetKind, Movement, TxContext

    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    dep_hist = list(bundle.deposits)
    wd_hist = list(bundle.withdrawals)

    def hist_match(hist: list[dict[str, Any]], cur: str, amount: Decimal, ts: int) -> dict[str, Any] | None:
        best = None
        for h in hist:
            if h["currency"] != cur:
                continue
            gross = Decimal(str(h["amount"])) + Decimal(str(h.get("fee") or 0))
            if amount not in (Decimal(str(h["amount"])), gross):
                continue
            gap = abs(int(h.get("update_time") or h["create_time"]) - ts)
            if best is None or gap < best[0]:
                best = (gap, h)
        if best:
            hist.remove(best[1])
            return best[1]
        return None

    for r in bundle.journal:
        jt = r["journal_type"]
        ts = int(r["event_timestamp_ms"]) // 1000
        qty = Decimal(r["transaction_qty"])
        cur = r["instrument_name"]
        meta: dict[str, Any] = {"journal_type": jt}
        if jt in ("TRADING", "TRADE_FEE"):
            h = f"cryptocom:order:{r['order_id']}"
            source = "fee" if jt == "TRADE_FEE" else "trade"
        else:
            h = f"cryptocom:{jt.lower()}:{r['journal_id']}"
            source = jt.lower()
            hist = hist_match(dep_hist if "DEPOSIT" in jt else wd_hist, cur, abs(qty), int(r["event_timestamp_ms"]))
            if hist:
                txh, idx = _split_txid(str(hist.get("txid") or ""))
                meta.update({"txid": txh, "txid_index": idx, "network": hist.get("network_id"),
                             "address": hist.get("address"), "fee": str(hist.get("fee") or 0),
                             "net_amount": str(hist["amount"])})
        def mk(leg: str, amount: Decimal, src: str) -> Movement:
            return Movement(chain=chain_name, tx_hash=h, leg_id=leg, block=0, timestamp=ts, wallet=wallet,
                            asset_id=cur, asset_kind=AssetKind.EXCHANGE, symbol=cur, decimals=8, amount=amount,
                            counterparty="crypto.com", source=src)

        fee = Decimal(str(meta.get("fee") or 0))
        if "WITHDRAWAL" in jt and fee > 0:  # journal amount = sent + network fee; the wallet receives only the net
            moves.append(mk(f"journal:{r['journal_id']}", qty + fee, source))
            moves.append(mk(f"journal:{r['journal_id']}:fee", -fee, "fee"))
        else:
            moves.append(mk(f"journal:{r['journal_id']}", qty, source))
        t = txs.get(h)
        if t is None:
            txs[h] = TxContext(chain=chain_name, tx_hash=h, block=0, timestamp=ts, tx_from=wallet,
                               tx_to="crypto.com", method_id="", function_name=jt.lower(), is_error=False,
                               gas_fee=Decimal(0), value=Decimal(0), meta=meta)
        else:
            t.timestamp = min(t.timestamp, ts)
            t.meta.update({k: v for k, v in meta.items() if k != "journal_type"})
    # trade price/pair info for order details
    for tr in bundle.trades:
        t = txs.get(f"cryptocom:order:{tr['order_id']}")
        if t is not None:
            t.meta.setdefault("instrument", tr["instrument_name"])
            t.meta.setdefault("fills", 0)
            t.meta["fills"] += 1
    moves.sort(key=lambda m: (m.timestamp, m.tx_hash, m.leg_id))
    return moves, txs
