"""Koios client (cached) and per-wallet Cardano fetch.

A Cardano "wallet" is a stake address. Rows in addresses.csv may be payment addresses
(addr1…), which are resolved to their stake address once (immutable, cached).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...config import ChainConfig
from ...http import HttpClient
from ...store import Store
from .address import parse_address

log = logging.getLogger(__name__)

TX_BATCH = 50
LIST_PAGE = 1000


class KoiosClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None, *,
                 refresh_tail: bool = False) -> None:
        self.url = chain.explorer_url
        self.store = store
        self.refresh_tail = refresh_tail
        key = chain.explorer_api_key()
        headers = {"User-Agent": "loledger/0.1"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = http or HttpClient(chain.explorer_rps, client=httpx.Client(timeout=60, headers=headers))
        self.source = "koios"

    def _call(self, method: str, endpoint: str, *, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None, cache_key: dict[str, Any] | None = None,
              tail: bool = False) -> Any:
        key = cache_key if cache_key is not None else {**(params or {}), **(body or {})}
        cached = self.store.get_raw(self.source, endpoint, key, allow_tail=not self.refresh_tail)
        if cached is not None:
            return cached
        res = self.http.request_json(method, f"{self.url}/{endpoint}", params=params, json_body=body)
        self.store.put_raw(self.source, endpoint, key, res, is_tail=tail)
        return res

    def stake_of(self, addresses: list[str]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for a in addresses:
            res = self._call("POST", "address_info", body={"_addresses": [a]})
            out[a] = res[0].get("stake_address") if res else None
        return out

    def account_txs(self, stake: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._call("GET", "account_txs", params={"_stake_address": stake, "offset": offset,
                                                            "limit": LIST_PAGE, "order": "block_height.asc"},
                              tail=True)
            rows.extend(page)
            if len(page) < LIST_PAGE:
                return rows
            offset += LIST_PAGE

    def reward_history(self, stake: str) -> list[dict[str, Any]]:
        """Per-epoch staking rewards credited to the stake address's reward account (all pools, all types)."""
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._call("POST", "account_reward_history", params={"offset": offset, "limit": LIST_PAGE},
                              body={"_stake_addresses": [stake]}, tail=True)
            rows.extend(page)
            if len(page) < LIST_PAGE:
                return rows
            offset += LIST_PAGE

    def tx_info(self, hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Full tx info (inputs, outputs, assets, metadata, withdrawals, certs). Cached per hash."""
        out: dict[str, dict[str, Any]] = {}
        todo: list[str] = []
        for h in dict.fromkeys(hashes):
            c = self.store.get_raw(self.source, "tx_info", {"tx": h})
            if c is not None:
                out[h] = c
            else:
                todo.append(h)
        for i in range(0, len(todo), TX_BATCH):
            chunk = todo[i:i + TX_BATCH]
            res = self.http.request_json("POST", f"{self.url}/tx_info", json_body={
                "_tx_hashes": chunk, "_inputs": True, "_metadata": True, "_assets": True,
                "_withdrawals": True, "_certs": True, "_scripts": True, "_bytecode": False})
            for t in res:
                self.store.put_raw(self.source, "tx_info", {"tx": t["tx_hash"]}, t)
                out[t["tx_hash"]] = t
            log.info("  tx_info %d/%d", min(i + TX_BATCH, len(todo)), len(todo))
        return out

    def datums(self, hashes: Iterable[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        todo: list[str] = []
        for h in dict.fromkeys(hashes):
            c = self.store.get_raw(self.source, "datum_info", {"datum": h})
            if c is not None:
                out[h] = c
            else:
                todo.append(h)
        for i in range(0, len(todo), TX_BATCH):
            res = self.http.request_json("POST", f"{self.url}/datum_info", json_body={"_datum_hashes": todo[i:i + TX_BATCH]})
            for d in res:
                self.store.put_raw(self.source, "datum_info", {"datum": d["datum_hash"]}, d.get("value"))
                out[d["datum_hash"]] = d.get("value")
        return out

    def spent_status(self, refs: Iterable[str]) -> dict[str, bool]:
        """is_spent for UTxO refs. Spent is final and cached; unspent is re-checked each run."""
        out: dict[str, bool] = {}
        todo: list[str] = []
        for r in dict.fromkeys(refs):
            if self.store.get_raw(self.source, "utxo_spent", {"ref": r}) is not None:
                out[r] = True
            else:
                todo.append(r)
        for i in range(0, len(todo), TX_BATCH):
            res = self.http.request_json("POST", f"{self.url}/utxo_info",
                                         json_body={"_utxo_refs": todo[i:i + TX_BATCH], "_extended": False})
            for u in res:
                ref = f"{u['tx_hash']}#{u['tx_index']}"
                spent = bool(u.get("is_spent"))
                out[ref] = spent
                if spent:
                    self.store.put_raw(self.source, "utxo_spent", {"ref": ref}, True)
        return out


MAX_CHASE_DEPTH = 6


def _is_script(io: dict[str, Any]) -> str | None:
    pa = parse_address(io["payment_addr"]["bech32"])
    return pa.payment_cred if pa and pa.is_script_payment else None


def _owned(io: dict[str, Any], stake: str) -> bool:
    pa = parse_address(io["payment_addr"]["bech32"])
    return bool(pa and pa.is_key_payment and io.get("stake_addr") == stake)


@dataclass
class CardanoRawBundle:
    stake: str
    txs: dict[str, dict[str, Any]]
    datums: dict[str, Any] = field(default_factory=dict)


class CardanoFetcher:
    def __init__(self, chain: ChainConfig, store: Store, *, refresh_tail: bool = False,
                 http: HttpClient | None = None) -> None:
        self.chain = chain
        self.koios = KoiosClient(chain, store, http, refresh_tail=refresh_tail)

    def resolve(self, addresses: list[str]) -> dict[str, str]:
        """Map each csv address to the wallet id (stake address; enterprise addresses map to themselves)."""
        out: dict[str, str] = {}
        need = [a for a in addresses if not a.startswith("stake1")]
        stakes = self.koios.stake_of(need) if need else {}
        for a in addresses:
            out[a] = a if a.startswith("stake1") else (stakes.get(a) or a)
        return out

    def chase_order_chains(self, stake: str, txs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Find intermediate txs (e.g. partial matches) that consumed our order UTxOs without paying us.

        Order UTxOs we created that are never spent inside our history are 'unresolved'. Payout txs in
        our history that spend a script UTxO of unknown origin (same script as an unresolved order) are
        walked backwards through the creating txs until one of them spends an unresolved order.
        """
        created: dict[str, str] = {}  # ref -> script hash
        for t in txs.values():
            if any(_owned(i, stake) for i in t["inputs"]):
                for o in t["outputs"]:
                    sh = _is_script(o)
                    if sh and (o.get("datum_hash") or o.get("inline_datum")):
                        created[f"{o['tx_hash']}#{o['tx_index']}"] = sh
        spent = {f"{i['tx_hash']}#{i['tx_index']}" for t in txs.values() for i in t["inputs"]}
        unresolved = {r: sh for r, sh in created.items() if r not in spent}
        if not unresolved:
            return {}
        scripts = set(unresolved.values())
        # (creator tx hash, path of txs walked so far)
        frontier: dict[str, list[str]] = {}
        for t in txs.values():
            if any(_owned(o, stake) for o in t["outputs"]):
                for i in t["inputs"]:
                    if _is_script(i) in scripts and i["tx_hash"] not in txs:
                        frontier.setdefault(i["tx_hash"], [])
        found: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for _ in range(MAX_CHASE_DEPTH):
            if not frontier or not unresolved:
                break
            fetched = self.koios.tx_info(list(frontier))
            nxt: dict[str, list[str]] = {}
            for h, t in fetched.items():
                seen.add(h)
                path = [*frontier.get(h, []), h]
                ins = {f"{i['tx_hash']}#{i['tx_index']}" for i in t["inputs"]}
                hit = ins & set(unresolved)
                if hit:
                    for ph in path:
                        found[ph] = fetched.get(ph) or self.koios.tx_info([ph])[ph]
                    for r in hit:
                        unresolved.pop(r, None)
                    continue
                for i in t["inputs"]:
                    if _is_script(i) in scripts and i["tx_hash"] not in txs and i["tx_hash"] not in seen:
                        nxt.setdefault(i["tx_hash"], path)
            frontier = nxt
        return found

    def fetch_wallet(self, stake: str) -> CardanoRawBundle:
        hashes = [r["tx_hash"] for r in self.koios.account_txs(stake)]
        txs = self.koios.tx_info(hashes)
        n_hist = len(txs)
        txs.update(self.chase_order_chains(stake, txs))
        if len(txs) > n_hist:
            log.info("  %s: +%d intermediate order txs (partial fills not paying this wallet)",
                     stake[:14], len(txs) - n_hist)
        datum_hashes = {o["datum_hash"] for t in txs.values() for o in t["outputs"]
                        if o.get("datum_hash") and not o.get("inline_datum")}
        datums = self.koios.datums(datum_hashes) if datum_hashes else {}
        log.info("  %s: %d txs, %d hashed datums", stake[:14], len(txs), len(datums))
        return CardanoRawBundle(stake=stake, txs=txs, datums=datums)
