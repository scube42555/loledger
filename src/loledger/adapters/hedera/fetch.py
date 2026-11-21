"""Hedera via the public mirror node REST API (keyless). History cached keyed by the newest tx."""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient
from ...store import Store

log = logging.getLogger(__name__)
BASE = "https://mainnet.mirrornode.hedera.com"


class HederaClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None) -> None:
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 5)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.request_json("GET", path if path.startswith("http") else f"{BASE}{path}", params=params)

    def history(self, account: str) -> list[dict[str, Any]]:
        newest = (self.get("/api/v1/transactions", {"account.id": account, "limit": 1, "order": "desc"}).get("transactions") or [{}])[0]
        key = {"account": account, "newest": newest.get("consensus_timestamp")}
        cached = self.store.get_raw("hedera-mirror", "transactions", key)
        if cached is not None:
            return list(cached)
        out: list[dict[str, Any]] = []
        url: str | None = "/api/v1/transactions"
        params: dict[str, Any] | None = {"account.id": account, "limit": 100, "order": "asc"}
        while url:
            b = self.get(url, params)
            out.extend(b.get("transactions") or [])
            nxt = (b.get("links") or {}).get("next")
            url, params = (f"{BASE}{nxt}" if nxt else None), None
        self.store.put_raw("hedera-mirror", "transactions", key, out)
        log.info("  hedera %s: %d txs", account, len(out))
        return out

    def token(self, token_id: str) -> tuple[str, int]:
        c = self.store.get_raw("hedera-mirror", "token", {"id": token_id})
        if c is None:
            t = self.get(f"/api/v1/tokens/{token_id}")
            c = [str(t.get("symbol") or token_id), int(t.get("decimals") or 0)]
            self.store.put_raw("hedera-mirror", "token", {"id": token_id}, c)
        return str(c[0]), int(c[1])

    def account(self, account: str) -> dict[str, Any]:
        return dict(self.get(f"/api/v1/accounts/{account}"))
