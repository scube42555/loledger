"""Algorand via the public Algonode indexer (keyless). Pages are cached by next-token (last = tail)."""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient
from ...store import Store

log = logging.getLogger(__name__)
IDX = "https://mainnet-idx.algonode.cloud/v2"


class AlgorandClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None, *, refresh_tail: bool = False) -> None:
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 5)
        self.refresh_tail = refresh_tail

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.request_json("GET", f"{IDX}{path}", params=params)

    def history(self, addr: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        nxt: str | None = None
        while True:
            key = {"addr": addr, "next": nxt}
            page = self.store.get_raw("algonode", "transactions", key, allow_tail=not self.refresh_tail)
            if page is None:
                params: dict[str, Any] = {"limit": 1000}
                if nxt:
                    params["next"] = nxt
                page = self.get(f"/accounts/{addr}/transactions", params)
                tail = len(page.get("transactions") or []) < 1000
                self.store.put_raw("algonode", "transactions", key, page, is_tail=tail)
            out.extend(page.get("transactions") or [])
            nxt = page.get("next-token")
            if not nxt or len(page.get("transactions") or []) < 1000:
                out.sort(key=lambda t: (t["confirmed-round"], t.get("intra-round-offset", 0)))
                log.info("  algorand %s…: %d txs", addr[:8], len(out))
                return out

    def asset(self, asset_id: int) -> tuple[str, int]:
        c = self.store.get_raw("algonode", "asset", {"id": asset_id})
        if c is None:
            p = (self.get(f"/assets/{asset_id}").get("asset") or {}).get("params") or {}
            c = [str(p.get("unit-name") or p.get("name") or asset_id), int(p.get("decimals") or 0)]
            self.store.put_raw("algonode", "asset", {"id": asset_id}, c)
        return str(c[0]), int(c[1])

    def account(self, addr: str) -> dict[str, Any]:
        return dict(self.get(f"/accounts/{addr}").get("account") or {})
