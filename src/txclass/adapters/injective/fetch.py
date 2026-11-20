"""Injective via the official explorer API (full history with per-message event logs) + LCD."""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient
from ...store import Store

log = logging.getLogger(__name__)
EXPLORER = "https://sentry.explorer.grpc-web.injective.network/api/explorer/v1"
LCD = "https://sentry.lcd.injective.network"


class InjectiveClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None) -> None:
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 3)

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.request_json("GET", url, params=params)

    def history(self, addr: str) -> list[dict[str, Any]]:
        first = self.get(f"{EXPLORER}/accountTxs/{addr}", {"skip": 0, "limit": 1})
        total = int((first.get("paging") or {}).get("total") or 0)
        key = {"addr": addr, "total": total}
        cached = self.store.get_raw("injective-explorer", "accountTxs", key)
        if cached is not None:
            return list(cached)
        out: list[dict[str, Any]] = []
        while len(out) < total:
            page = self.get(f"{EXPLORER}/accountTxs/{addr}", {"skip": len(out), "limit": 100}).get("data") or []
            if not page:
                break
            out.extend(page)
        out.sort(key=lambda t: (int(t["block_number"]), str(t["hash"])))
        self.store.put_raw("injective-explorer", "accountTxs", key, out)
        log.info("  injective %s…: %d txs", addr[:10], len(out))
        return out

    def module_accounts(self) -> dict[str, str]:
        c = self.store.get_raw("injective-lcd", "module_accounts", {})
        if c is None:
            accs = self.get(f"{LCD}/cosmos/auth/v1beta1/module_accounts").get("accounts", [])
            c = {(a.get("base_account") or {}).get("address") or a.get("address"): a.get("name") for a in accs}
            self.store.put_raw("injective-lcd", "module_accounts", {}, c)
        return dict(c)

    def balances(self, addr: str) -> dict[str, int]:
        bal = self.get(f"{LCD}/cosmos/bank/v1beta1/balances/{addr}").get("balances") or []
        return {b["denom"]: int(b["amount"]) for b in bal}
