"""UTXO chains (Bitcoin, Dogecoin, Kaspa): keyless public APIs, normalized to one tx shape.

  bitcoin  mempool.space (Esplora)  /api/address/:a/txs, /txs/chain/:last_txid
  doge     BlockCypher              /v1/doge/main/addrs/:a/full?limit=50&before=<height>
  kaspa    api.kaspa.org            /addresses/:a/full-transactions?limit&offset&resolve_previous_outpoints=light

Common tx: {"txid", "ts", "height", "inputs": [{"addr", "value"}], "outputs": [{"addr", "value", "n"}], "fee"}
(values in base units: sats / koinu / sompi). A wallet's full history is cached keyed by the
address's current tx count, so it is re-read only when new transactions appear.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient
from ...store import Store

log = logging.getLogger(__name__)

UTx = dict[str, Any]


class UtxoClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None) -> None:
        self.chain = chain
        self.store = store
        self.http = http or HttpClient(chain.explorer_rps or 1)
        self.provider = chain.explorer  # mempool | blockcypher | kaspa

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.request_json("GET", url, params=params)

    # -- tx count / balance (live, uncached) --
    def stats(self, addr: str) -> tuple[int, int]:
        """(tx_count, balance in base units) from the provider, uncached."""
        if self.provider == "mempool":
            s = self.get(f"https://mempool.space/api/address/{addr}")
            cs = s["chain_stats"]
            return int(cs["tx_count"]), int(cs["funded_txo_sum"]) - int(cs["spent_txo_sum"])
        if self.provider == "blockcypher":
            s = self.get(f"https://api.blockcypher.com/v1/doge/main/addrs/{addr}/balance")
            return int(s["final_n_tx"]), int(s["final_balance"])
        if self.provider == "kaspa":
            n = self.get(f"https://api.kaspa.org/addresses/{addr}/transactions-count")["total"]
            b = self.get(f"https://api.kaspa.org/addresses/{addr}/balance")["balance"]
            return int(n), int(b)
        raise ValueError(f"unknown UTXO provider {self.provider!r}")

    def history(self, addr: str) -> list[UTx]:
        n, _ = self.stats(addr)
        key = {"addr": addr, "tx_count": n}
        cached = self.store.get_raw(self.provider, "history", key)
        if cached is not None:
            return list(cached)
        txs = {"mempool": self._mempool, "blockcypher": self._blockcypher, "kaspa": self._kaspa}[self.provider](addr)
        txs.sort(key=lambda t: (t["ts"], t["txid"]))
        self.store.put_raw(self.provider, "history", key, txs)
        log.info("  %s %s…: %d txs", self.chain.name, addr[:12], len(txs))
        return txs

    def _mempool(self, addr: str) -> list[UTx]:
        out: list[UTx] = []
        page = self.get(f"https://mempool.space/api/address/{addr}/txs")
        while True:
            confirmed = [t for t in page if t["status"].get("confirmed")]
            out += [{"txid": t["txid"], "ts": int(t["status"]["block_time"]), "height": int(t["status"]["block_height"]),
                     "fee": int(t.get("fee") or 0),
                     "inputs": [{"addr": ("coinbase" if v.get("is_coinbase") else (v.get("prevout") or {}).get("scriptpubkey_address", "")),
                                 "value": int((v.get("prevout") or {}).get("value") or 0)} for v in t["vin"]],
                     "outputs": [{"addr": o.get("scriptpubkey_address", ""), "value": int(o["value"]), "n": n}
                                 for n, o in enumerate(t["vout"])]} for t in confirmed]
            if len(page) < 25 or not confirmed:
                return out
            page = self.get(f"https://mempool.space/api/address/{addr}/txs/chain/{confirmed[-1]['txid']}")

    def _blockcypher(self, addr: str) -> list[UTx]:
        out: list[UTx] = []
        params: dict[str, Any] = {"limit": 50}
        while True:
            page = self.get(f"https://api.blockcypher.com/v1/doge/main/addrs/{addr}/full", params)
            txs = [t for t in page.get("txs", []) if t.get("block_height", -1) >= 0]
            for t in txs:
                out.append({"txid": t["hash"], "ts": _iso(t.get("confirmed") or t["received"]), "height": int(t["block_height"]),
                            "fee": int(t.get("fees") or 0),
                            "inputs": [{"addr": (i.get("addresses") or ["coinbase"])[0], "value": int(i.get("output_value") or 0)}
                                       for i in t["inputs"]],
                            "outputs": [{"addr": (o.get("addresses") or [""])[0], "value": int(o["value"]), "n": n}
                                        for n, o in enumerate(t["outputs"])]})
            if not page.get("hasMore") or not txs:
                return out
            params = {"limit": 50, "before": min(int(t["block_height"]) for t in txs)}

    def _kaspa(self, addr: str) -> list[UTx]:
        out: list[UTx] = []
        offset = 0
        while True:
            page = self.get(f"https://api.kaspa.org/addresses/{addr}/full-transactions",
                            {"limit": 50, "offset": offset, "resolve_previous_outpoints": "light"})
            for t in page:
                if not t.get("is_accepted"):
                    continue
                out.append({"txid": t["transaction_id"], "ts": int(t["block_time"]) // 1000,
                            "height": int(t.get("accepting_block_blue_score") or 0), "fee": None,
                            "inputs": [{"addr": i.get("previous_outpoint_address") or "", "value": int(i.get("previous_outpoint_amount") or 0)}
                                       for i in t.get("inputs") or []],
                            "outputs": [{"addr": o.get("script_public_key_address") or "", "value": int(o["amount"]), "n": int(o.get("index", n))}
                                        for n, o in enumerate(t.get("outputs") or [])]})
            if len(page) < 50:
                return out
            offset += 50


def _iso(s: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
