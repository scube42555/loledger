"""EVM fetching: Etherscan-compatible explorer (Etherscan V2 / Routescan) + JSON-RPC, all cached.

Pagination: the explorer returns at most PAGE rows per call and rows carry no log index, so
overlapping pages cannot be de-duplicated safely (two identical transfers in one tx are legit).
Instead each full page is cut before its last block and the next request starts *at* that
block, so every block is read from exactly one response. A page whose rows are all in one block
is expanded with page=2,3,... on a single-block range.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from ...config import ChainConfig, ConfigError
from ...http import HttpClient, TransientError
from ...store import Store
from .atomic import AtomicChange, find_atomic_changes

log = logging.getLogger(__name__)

PAGE = 1000
END_BLOCK = 999_999_999
ACCOUNT_ACTIONS = ("txlist", "txlistinternal", "tokentx", "tokennfttx", "token1155tx")
EMPTY_MESSAGES = ("no transactions found", "no records found", "no token transfers found")
HEAD_MARGIN = 64

Row = dict[str, Any]


class ExplorerError(Exception):
    pass


def check_explorer_body(body: Any) -> None:
    """Raise TransientError for retryable API-level failures, ExplorerError for hard ones."""
    if not isinstance(body, dict):
        raise ExplorerError(f"unexpected response: {str(body)[:200]}")
    if "jsonrpc" in body:  # proxy module
        return
    if str(body.get("status")) == "1":
        return
    msg = str(body.get("message", "")).lower()
    result = body.get("result")
    if result == [] or any(m in msg for m in EMPTY_MESSAGES):
        return
    text = f"{msg} {result}".lower()
    if "rate limit" in text or "too many" in text or "timeout" in text or "try again" in text:
        raise TransientError(text[:200])
    raise ExplorerError(f"{body.get('message')}: {str(result)[:300]}")


class ExplorerClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient, *,
                 refresh_tail: bool = False) -> None:
        self.chain = chain
        self.store = store
        self.http = http
        self.refresh_tail = refresh_tail
        self.source = f"{chain.explorer}:{chain.chain_id}"
        self._api_key = chain.explorer_api_key()

    def _get(self, params: dict[str, Any], *, tail: Callable[[list[Row]], bool] | None = None) -> list[Row]:
        result = self._get_result(params, tail=tail)
        return result if isinstance(result, list) else []

    def _get_result(self, params: dict[str, Any], *,
                    tail: Callable[[list[Row]], bool] | None = None) -> Any:
        endpoint = f"{params['module']}.{params['action']}"
        cached = self.store.get_raw(self.source, endpoint, params, allow_tail=not self.refresh_tail)
        if cached is not None:
            return cached
        q = dict(params)
        if self.chain.explorer == "etherscan":
            q["chainid"] = self.chain.chain_id
        if self._api_key:
            q["apikey"] = self._api_key
        body = self.http.request_json("GET", self.chain.explorer_url, params=q, check=check_explorer_body)
        result = body.get("result")
        if isinstance(result, list):
            self.store.put_raw(self.source, endpoint, params, result, is_tail=bool(tail and tail(result)))
            return result
        if result is None or (isinstance(result, str) and not result.strip()):
            return []
        self.store.put_raw(self.source, endpoint, params, result)
        return result

    def balance_at(self, address: str, block: int) -> int:
        """Native balance (wei) at the end of `block`. Immutable for past blocks, so cached."""
        if block <= 0:
            return 0
        res = self._get_result({"module": "account", "action": "balancehistory", "address": address,
                                "blockno": block})
        return int(res)

    def account_rows(self, action: str, address: str) -> list[Row]:
        rows: list[Row] = []
        start = 0
        while True:
            params = {"module": "account", "action": action, "address": address,
                      "startblock": start, "endblock": END_BLOCK, "page": 1, "offset": PAGE,
                      "sort": "asc"}
            page = self._get(params, tail=lambda r: len(r) < PAGE)
            if len(page) < PAGE:
                rows.extend(page)
                return rows
            first, last = int(page[0]["blockNumber"]), int(page[-1]["blockNumber"])
            if first == last:
                rows.extend(self._single_block(action, address, last))
                start = last + 1
            else:
                rows.extend(r for r in page if int(r["blockNumber"]) < last)
                start = last

    def _single_block(self, action: str, address: str, block: int) -> list[Row]:
        rows: list[Row] = []
        page_no = 1
        while True:
            params = {"module": "account", "action": action, "address": address,
                      "startblock": block, "endblock": block, "page": page_no, "offset": PAGE,
                      "sort": "asc"}
            page = self._get(params)
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            page_no += 1

    def mined_blocks(self, address: str) -> list[Row]:
        """Blocks where `address` was the fee recipient (proposer rewards). Paged by page number."""
        rows: list[Row] = []
        page = 1
        while True:
            got = self._get({"module": "account", "action": "getminedblocks", "address": address,
                             "blocktype": "blocks", "page": page, "offset": PAGE},
                            tail=lambda r: len(r) < PAGE)
            rows.extend(got)
            if len(got) < PAGE:
                return rows
            page += 1

    def contract_name(self, address: str) -> str:
        rows = self._get({"module": "contract", "action": "getsourcecode", "address": address})
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("ContractName") or "")
        return ""


class RpcClient:
    """Batched JSON-RPC with per-call caching (immutable results only)."""

    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient) -> None:
        if not chain.rpc_url:
            raise ValueError(f"no rpc_url configured for {chain.name}")
        self.url = chain.rpc_url
        self.store = store
        self.http = http
        self.source = f"rpc:{chain.chain_id}"
        self.batch = max(1, chain.rpc_batch)

    def call_many(self, method: str, keys: Iterable[str],
                  params_for: Callable[[str], list[Any]]) -> dict[str, Any]:
        """Return {key: result}. Cached results are reused; null results are not cached."""
        out: dict[str, Any] = {}
        todo: list[str] = []
        nulls = 0
        for k in dict.fromkeys(keys):
            cached = self.store.get_raw(self.source, method, {"key": k})
            if cached is not None:
                out[k] = cached
            else:
                todo.append(k)
        i = 0
        while i < len(todo):
            chunk = todo[i:i + self.batch]
            payload = [{"jsonrpc": "2.0", "id": j, "method": method, "params": params_for(k)}
                       for j, k in enumerate(chunk)]
            try:
                body = self.http.request_json("POST", self.url, json_body=payload, check=_check_rpc_batch)
            except httpx.HTTPStatusError as e:
                limit = re.search(r"more than (\d+) requests", e.response.text)
                if e.response.status_code == 400 and limit and int(limit.group(1)) < self.batch:
                    self.batch = max(1, int(limit.group(1)))
                    log.warning("  %s caps batches at %d; shrinking", self.url, self.batch)
                    continue
                raise
            i += len(chunk)
            by_id = {r.get("id"): r for r in body} if isinstance(body, list) else {}
            for j, k in enumerate(chunk):
                r = by_id.get(j)
                if r is None or "error" in r:
                    r = self.http.request_json("POST", self.url, json_body=payload[j], check=_check_rpc_one)
                result = r.get("result")
                if result is not None:
                    self.store.put_raw(self.source, method, {"key": k}, result)
                    out[k] = result
                else:
                    nulls += 1
            if (i // self.batch) % 10 == 9:
                log.info("  rpc %s: %d/%d", method, min(i + self.batch, len(todo)), len(todo))
        if nulls:
            log.warning("  %s returned null for %d/%d %s calls (pruned node? check rpc_url)",
                        self.url, nulls, len(todo), method)
        return out


def _rate_limited(err: Any) -> bool:
    text = str(err).lower()
    return "limit" in text or "too many" in text or "-32005" in text


def _check_rpc_batch(body: Any) -> None:
    if isinstance(body, dict) and "error" in body and _rate_limited(body["error"]):
        raise TransientError(str(body["error"]))
    if isinstance(body, list) and body and all("error" in r and _rate_limited(r["error"]) for r in body):
        raise TransientError("batch rate limited")


def _check_rpc_one(body: Any) -> None:
    if isinstance(body, dict) and "error" in body:
        if _rate_limited(body["error"]):
            raise TransientError(str(body["error"]))
        raise ExplorerError(f"rpc error: {body['error']}")


@dataclass
class EvmRawBundle:
    wallet: str
    rows: dict[str, list[Row]]
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    txs: dict[str, dict[str, Any]] = field(default_factory=dict)  # only for hashes not in txlist
    atomic: list[AtomicChange] = field(default_factory=list)  # Avalanche C<->X/P transfers
    block_ts: dict[int, int] = field(default_factory=dict)  # timestamps for atomic blocks

    def all_hashes(self) -> list[str]:
        seen: dict[str, None] = {}
        for action in ACCOUNT_ACTIONS:
            for r in self.rows.get(action, []):
                seen[str(r["hash"]).lower()] = None
        return list(seen)


class EvmFetcher:
    def __init__(self, chain: ChainConfig, store: Store, *, refresh_tail: bool = False,
                 explorer_http: HttpClient | None = None, rpc_http: HttpClient | None = None) -> None:
        self.chain = chain
        self.store = store
        self.explorer = ExplorerClient(chain, store, explorer_http or HttpClient(chain.explorer_rps),
                                       refresh_tail=refresh_tail)
        self.rpc = RpcClient(chain, store, rpc_http or HttpClient(chain.rpc_rps))
        self.beacon: ExplorerClient | None = None
        if chain.beacon_withdrawals:
            try:
                ec = dataclasses.replace(chain, explorer="etherscan")
                self.beacon = ExplorerClient(ec, store, HttpClient(3), refresh_tail=refresh_tail)
            except ConfigError as e:
                log.warning("%s: beacon withdrawals skipped (%s)", chain.name, e)

    def fetch_wallet(self, wallet: str) -> EvmRawBundle:
        rows = {}
        for action in ACCOUNT_ACTIONS:
            rows[action] = self.explorer.account_rows(action, wallet)
            log.info("  %s %s: %d rows", wallet[:10], action, len(rows[action]))
        if self.beacon is not None:
            rows["beacon"] = self.beacon.account_rows("txsBeaconWithdrawal", wallet)
            rows["proposer"] = [r for r in self.beacon.mined_blocks(wallet) if int(r.get("blockReward") or 0) > 0]
            if rows["beacon"] or rows["proposer"]:
                log.info("  %s beacon withdrawals: %d, proposer rewards: %d", wallet[:10], len(rows["beacon"]),
                         len(rows["proposer"]))
        bundle = EvmRawBundle(wallet=wallet, rows=rows)
        hashes = bundle.all_hashes()
        bundle.receipts = self.rpc.call_many("eth_getTransactionReceipt", hashes, lambda h: [h])
        in_txlist = {str(r["hash"]).lower() for r in rows["txlist"]}
        missing = [h for h in hashes if h not in in_txlist]
        bundle.txs = self.rpc.call_many("eth_getTransactionByHash", missing, lambda h: [h])
        log.info("  %s receipts: %d, external-sender txs: %d", wallet[:10], len(bundle.receipts), len(missing))
        return bundle

    def atomic_changes(self, wallet: str, native_deltas: dict[int, Decimal]) -> tuple[list[AtomicChange], dict[int, int]]:
        """Detect off-EVM native balance changes (Avalanche atomic txs) by reconciliation."""
        head_hex = self.rpc.http.request_json(
            "POST", self.rpc.url, json_body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        # stay a little behind the head so the explorer's balance index has caught up
        head = int(head_hex["result"], 16) - HEAD_MARGIN
        changes = find_atomic_changes(native_deltas, lambda b: self.explorer.balance_at(wallet, b), head,
                                      self.chain.native_decimals)
        blocks = self.rpc.call_many("eth_getBlockByNumber", [hex(c.block) for c in changes], lambda b: [b, False])
        ts = {int(k, 16): int(v["timestamp"], 16) for k, v in blocks.items()}
        for c in changes:
            log.info("  %s atomic %s %s at block %d", wallet[:10], "import" if c.amount > 0 else "export",
                     c.amount, c.block)
        return changes, ts

    def enrich_addresses(self, addresses: Iterable[str], *, names: bool = True) -> None:
        """Record is_contract (eth_getCode) and verified contract name for counterparties."""
        known = self.store.get_address_info(self.chain.name)
        todo = [a for a in dict.fromkeys(addresses)
                if a.startswith("0x") and len(a) == 42 and known.get(a, (None, ""))[0] is None]
        codes = self.rpc.call_many("eth_getCode", todo, lambda a: [a, "latest"])
        for a, code in codes.items():
            self.store.set_address_info(self.chain.name, a, is_contract=code not in ("0x", "0x0", ""))
        self.store.commit()
        if not names:
            return
        info = self.store.get_address_info(self.chain.name)
        unnamed = [a for a, (is_c, name) in info.items() if is_c and not name]
        for n, a in enumerate(unnamed, 1):
            self.store.set_address_info(self.chain.name, a, contract_name=self.explorer.contract_name(a) or "")
            if n % 25 == 0:
                log.info("  contract names: %d/%d", n, len(unnamed))
                self.store.commit()
        self.store.commit()

