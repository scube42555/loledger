"""Sui via the public GraphQL service (JSON-RPC on public fullnodes was retired in 2026).

Verified on mainnet (2026-11-18): availableRange for transactions(filter: affectedAddress) starts at
checkpoint 0, maxPageSize 50. History pages are cached by cursor; the last page is a tail page.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient, TransientError
from ...store import Store

log = logging.getLogger(__name__)

URL = "https://graphql.mainnet.sui.io/graphql"
TX_QUERY = """
query ($address: SuiAddress!, $after: String) {
  transactions(first: 50, after: $after, filter: { affectedAddress: $address }) {
    pageInfo { hasNextPage endCursor }
    nodes {
      digest
      sender { address }
      kind { __typename ... on ProgrammableTransaction { commands(first: 50) { nodes { __typename
        ... on MoveCallCommand { function { name module { name package { address } } } } } } } }
      effects {
        status timestamp checkpoint { sequenceNumber }
        gasEffects { gasSummary { computationCost storageCost storageRebate nonRefundableStorageFee } }
        balanceChanges(first: 50) { pageInfo { hasNextPage endCursor } nodes { owner { address } coinType { repr } amount } }
        events(first: 50) { nodes { contents { type { repr } json } } }
      }
    }
  }
}"""


MORE_CHANGES = """
query ($d: String!, $after: String) {
  transaction(digest: $d) { effects { balanceChanges(first: 50, after: $after) {
    pageInfo { hasNextPage endCursor } nodes { owner { address } coinType { repr } amount } } } }
}"""
MAX_CHANGE_PAGES = 200  # mass airdrops: up to 10,000 recipients per tx


def _check(body: Any) -> None:
    errs = body.get("errors") if isinstance(body, dict) else None
    if errs:
        text = str(errs)
        if "rate" in text.lower() or "too many" in text.lower() or "timeout" in text.lower():
            raise TransientError(text[:200])
        raise RuntimeError(f"Sui GraphQL error: {text[:300]}")


class SuiClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None, *,
                 refresh_tail: bool = False) -> None:
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 1)
        self.refresh_tail = refresh_tail

    def query(self, q: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = self.http.request_json("POST", URL, json_body={"query": q, "variables": variables}, check=_check)
        return dict(body["data"])

    def history(self, addr: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            key = {"addr": addr, "after": after}
            page = self.store.get_raw("sui-graphql", "transactions", key, allow_tail=not self.refresh_tail)
            if page is None:
                page = self.query(TX_QUERY, {"address": addr, "after": after})["transactions"]
                self.store.put_raw("sui-graphql", "transactions", key, page, is_tail=not page["pageInfo"]["hasNextPage"])
            for node in page["nodes"]:
                self._complete_balance_changes(node)
            out.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                log.info("  sui %s…: %d txs", addr[:10], len(out))
                return out
            after = page["pageInfo"]["endCursor"]

    def _complete_balance_changes(self, node: dict[str, Any]) -> None:
        """A tx with >50 balance changes (e.g. a mass airdrop) needs the remaining pages, else the wallet's
        own change can be missing. Fetched once per tx digest and cached."""
        bc = node["effects"]["balanceChanges"]
        info = bc.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return
        cached = self.store.get_raw("sui-graphql", "balanceChanges", {"d": node["digest"]})
        if cached is None:
            rest: list[dict[str, Any]] = []
            after = info.get("endCursor")
            for _ in range(MAX_CHANGE_PAGES):
                page = self.query(MORE_CHANGES, {"d": node["digest"], "after": after})["transaction"]["effects"]["balanceChanges"]
                rest.extend(page["nodes"])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            else:
                log.warning("  sui tx %s…: more than %d balance-change pages; truncated", node["digest"][:10], MAX_CHANGE_PAGES)
            cached = rest
            self.store.put_raw("sui-graphql", "balanceChanges", {"d": node["digest"]}, rest)
        bc["nodes"] = bc["nodes"] + list(cached)
        bc["pageInfo"] = {"hasNextPage": False}

    def coin_metadata(self, coin_type: str) -> tuple[str, int]:
        c = self.store.get_raw("sui-graphql", "coinMetadata", {"t": coin_type})
        if c is None:
            m = self.query("query($t:String!){ coinMetadata(coinType:$t){ symbol decimals } }", {"t": coin_type}).get("coinMetadata")
            c = [str((m or {}).get("symbol") or coin_type.split("::")[-1]), int((m or {}).get("decimals") or 0)]
            self.store.put_raw("sui-graphql", "coinMetadata", {"t": coin_type}, c)
        return str(c[0]), int(c[1])

    def balances(self, addr: str) -> dict[str, int]:
        d = self.query("query($a:SuiAddress!){ address(address:$a){ balances(first:50){ nodes{ coinType{ repr } totalBalance } } } }",
                       {"a": addr})
        return {n["coinType"]["repr"]: int(n["totalBalance"]) for n in ((d.get("address") or {}).get("balances") or {}).get("nodes", [])}
