"""Gemini account activity through the REST API (read-only "Auditor" key: GEMINI_API_KEY / GEMINI_API_SECRET).

Gemini's transaction-history export stays the base (it has every record since the account opened, with running
balances for a self-check). The API adds what happened after the export was made:
- /v1/mytrades (all pairs, since a timestamp): trade id `tid` = the export's "Trade ID";
- /v1/transfers: deposits / withdrawals (crypto with tx hash, ACH / wire) and internal credits ("Staking Yield",
  the export's bare "Deposit" rows); times match the export to the millisecond.
Records are fetched from a few days before the export's last row, accumulated in the store (by tid / eid) so
history stays complete across syncs, and files.parse_gemini adds only those the export doesn't already have.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

import httpx

from ...config import ConfigError
from ...http import TransientError
from ...store import Store

log = logging.getLogger(__name__)
SOURCE = "gemini-api"
BASE = "https://api.gemini.com"
OVERLAP_MS = 3 * 86_400_000  # re-read a few days before the newest known record


def has_keys() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_API_SECRET"))


def sign(secret: str, payload_b64: bytes) -> str:
    return hmac.new(secret.encode(), payload_b64, hashlib.sha384).hexdigest()


class GeminiClient:
    def __init__(self, store: Store, client: httpx.Client | None = None) -> None:
        if not has_keys():
            raise ConfigError("GEMINI_API_KEY / GEMINI_API_SECRET not set (.env)")
        self.key, self.secret = os.environ["GEMINI_API_KEY"], os.environ["GEMINI_API_SECRET"]
        self.store = store
        self.client = client or httpx.Client(timeout=90)
        self._nonce = 0

    def post(self, path: str, **params: Any) -> Any:
        for attempt in range(5):
            self._nonce = max(self._nonce + 1, int(time.time() * 1000))  # must increase on every request
            payload = base64.b64encode(json.dumps({"request": path, "nonce": str(self._nonce), **params}).encode())
            try:
                r = self.client.post(BASE + path, headers={
                    "Content-Type": "text/plain", "Content-Length": "0", "Cache-Control": "no-cache",
                    "X-GEMINI-APIKEY": self.key, "X-GEMINI-PAYLOAD": payload.decode(),
                    "X-GEMINI-SIGNATURE": sign(self.secret, payload)})
                if r.status_code == 429 or r.status_code >= 500:
                    raise TransientError(f"HTTP {r.status_code}")
                body = r.json()
                if isinstance(body, dict) and body.get("result") == "error":
                    raise ValueError(f"Gemini API error: {body.get('reason')}: {body.get('message')}")
                time.sleep(0.25)  # private endpoints: 600 requests/min
                return body
            except (TransientError, httpx.TransportError) as e:
                if attempt == 4:
                    raise
                log.warning("  gemini %s: retry after %s", path, e)
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def _since(self, path: str, key: str, limit_param: str, limit: int, since_ms: int) -> list[dict[str, Any]]:
        """All records at or after since_ms (results are newest first and capped at `limit` per call)."""
        out: dict[str, dict[str, Any]] = {}
        t = since_ms
        while True:
            rows = self.post(path, timestamp=t // 1000, **{limit_param: limit}) or []
            new = [r for r in rows if str(r[key]) not in out]
            out.update({str(r[key]): r for r in rows})
            if len(rows) < limit or not new:
                return list(out.values())
            t = max(int(r["timestampms"]) for r in rows)  # a full page: continue after its newest record

    def _accumulate(self, endpoint: str, rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        seen = {str(r[key]): r for r in (self.store.get_raw(SOURCE, endpoint, {}) or [])}
        new = sum(1 for r in rows if str(r[key]) not in seen)
        seen.update({str(r[key]): r for r in rows})
        merged = sorted(seen.values(), key=lambda r: (int(r["timestampms"]), str(r[key])))
        self.store.put_raw(SOURCE, endpoint, {}, merged)
        log.info("  gemini %s: %d fetched, %d new, %d kept", endpoint, len(rows), new, len(merged))
        return merged

    def stored(self) -> dict[str, list[dict[str, Any]]]:
        return {k: list(self.store.get_raw(SOURCE, k, {}) or []) for k in ("trades", "transfers")}

    def fetch(self, since_ms: int) -> dict[str, list[dict[str, Any]]]:
        """since_ms: the export's last record; also continues from what earlier syncs stored."""
        have = self.stored()
        newest = max([since_ms] + [int(r["timestampms"]) for v in have.values() for r in v])
        start = max(0, newest - OVERLAP_MS)
        trades = self._since("/v1/mytrades", "tid", "limit_trades", 500, start)
        transfers = [t for t in self._since("/v1/transfers", "eid", "limit_transfers", 50, start)]
        return {"trades": self._accumulate("trades", trades, "tid"),
                "transfers": self._accumulate("transfers", transfers, "eid")}

    def balances(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(Decimal)
        for b in self.post("/v1/balances"):
            out[b["currency"]] += Decimal(b["amount"])
        return {k: v for k, v in out.items() if v}


QUOTES = ("USDT", "USDC", "GUSD", "DAI", "USD", "EUR", "GBP", "SGD", "AUD", "CAD", "BTC", "ETH")


def split_symbol(symbol: str) -> tuple[str, str]:
    """'ETHUSD' -> ('ETH', 'USD'), 'BTCUSDT' -> ('BTC', 'USDT'), 'SOLETH' -> ('SOL', 'ETH')."""
    s = symbol.upper()
    for q in sorted(QUOTES, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, ""
