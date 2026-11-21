"""Cosmos SDK chains (Cosmos Hub, dYdX) through public archive nodes.

Public nodes prune history (about the last year), so these use archive nodes, which keep everything:
- **Cosmos Hub:** the one public archive node (CryptoCrew) has address search disabled. The wallet's txs come
  from a list the user copies from an explorer (`export:` in settings.yaml: block height + the explorer's
  shortened hash). Each is resolved to its full hash by hashing the block's txs (RPC /block; the tx hash is
  sha256 of the tx bytes), then fetched with REST GetTx.
- **dYdX:** the archive nodes (Polkachu, KingNodes) allow tx search, so history is found by address. A list is
  still read when present, for txs search can't find (e.g. the bridge acknowledgement, signed by a block
  proposer), and its hashes are full.
Txs are immutable and cached forever; search results are cached too and re-run with --refresh. Current
balances and module accounts come from a node that serves current state.

dYdX's Ethereum bridge credits no tx: a proposer's MsgAcknowledgeBridges records the deposit, and the coins
arrive `delay_blocks` later in the end-blocker. `bridge_credits` rebuilds those credits (amount from the
acknowledged event, time from the completion block).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ...config import ChainConfig, ConfigError
from ...http import HttpClient
from ...store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    archive_rest: str
    state_rest: str  # current state (balances, module accounts, denom metadata)
    native_denom: str
    slug: str  # pseudo-address prefix, e.g. "cosmos:not_bonded_pool"
    leg_prefix: str
    archive_rpc: str = ""  # needed only to resolve shortened hashes
    search: bool = False


PROFILES = {
    "Cosmos": Profile("https://rest.cosmoshub-main.ccvalidators.com", "https://cosmos-rest.publicnode.com", "uatom",
                      "cosmos", "atom", archive_rpc="https://rpc.cosmoshub-main.ccvalidators.com"),
    "dYdX": Profile("https://dydx-dao-archive-api.polkachu.com", "https://dydx-dao-archive-api.polkachu.com", "adydx",
                    "dydx", "dydx", search=True),
}
# queries that together find every tx touching an address (deduplicated by hash)
SEARCH_QUERIES = ("message.sender='{a}'", "transfer.recipient='{a}'", "transfer.sender='{a}'",
                  "coin_received.receiver='{a}'", "coin_spent.spender='{a}'")


@dataclass(frozen=True)
class TxRef:
    wallet: str
    height: int  # 0 when the hash is complete (no block lookup needed)
    hash: str  # full hash, or the explorer's shortened "PREFIX...SUFFIX"
    note: str = ""

    @property
    def is_full(self) -> bool:
        return "..." not in self.hash.replace("…", "...") and len(self.hash) == 64

    def matches(self, full: str) -> bool:
        h = self.hash.upper().replace("…", "...")
        if "..." not in h:
            return full == h
        pre, suf = h.split("...", 1)
        return full.startswith(pre) and full.endswith(suf)


def read_tx_list(path: Path, required: bool = True) -> list[TxRef]:
    """CSV with Address, Height, Hash (full, or 'PREFIX...SUFFIX' with its height), optionally Explorer (a note)."""
    if not path.exists():
        if required:
            raise ConfigError(f"transaction list {path} not found (see the chain's block in settings.yaml)")
        return []
    out = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            height = (r.get("height") or "").replace(" ", "").replace(",", "")
            if r.get("address") and r.get("hash") and (height or len(r["hash"]) == 64):
                out.append(TxRef(r["address"], int(height or 0), r["hash"], r.get("explorer", "")))
    return out


class CosmosClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None, refresh: bool = False) -> None:
        if chain.name not in PROFILES:
            raise ConfigError(f"no Cosmos SDK profile for chain {chain.name!r} (known: {', '.join(PROFILES)})")
        self.chain = chain
        self.p = PROFILES[chain.name]
        self.store = store
        self.refresh = refresh
        self.http = http or HttpClient(chain.rpc_rps or 2)
        self.src = f"{self.p.slug}-archive"

    def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        for attempt in range(4):  # archive nodes now and then answer 200 with an empty body
            try:
                if headers:  # historical state query (x-cosmos-block-height): plain client, rate-limited
                    self.http.limiter.wait()
                    r = self.http.client.get(url, params=params, headers=headers)
                    r.raise_for_status()
                    return r.json()
                return self.http.request_json("GET", url, params=params)
            except (ValueError, httpx.HTTPError):
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    # -- txs --------------------------------------------------------------------------------------------
    def full_hash(self, ref: TxRef) -> str:
        if ref.is_full:
            return ref.hash.upper()
        key = {"height": ref.height, "hash": ref.hash.upper()}
        c = self.store.get_raw(self.src, "full_hash", key)
        if c is not None:
            return str(c)
        if not self.p.archive_rpc:
            raise ValueError(f"{ref.hash}: give the full hash (no archive RPC to resolve a shortened one)")
        blk = self.get(f"{self.p.archive_rpc}/block", {"height": ref.height})["result"]["block"]
        hashes = [hashlib.sha256(base64.b64decode(t)).hexdigest().upper() for t in blk["data"]["txs"] or []]
        found = [h for h in hashes if ref.matches(h)]
        if len(found) != 1:
            raise ValueError(f"block {ref.height}: {len(found)} txs match {ref.hash} ({len(hashes)} txs in the block); "
                             "check the height and hash in the transaction list")
        self.store.put_raw(self.src, "full_hash", key, found[0])
        return found[0]

    def tx(self, full: str) -> dict[str, Any]:
        c = self.store.get_raw(self.src, "tx", {"hash": full})
        if c is not None:
            return dict(c)
        t = self.get(f"{self.p.archive_rest}/cosmos/tx/v1beta1/txs/{full}")
        if "tx_response" not in t:
            raise ValueError(f"tx {full}: unexpected response {str(t)[:120]}")
        self.store.put_raw(self.src, "tx", {"hash": full}, t)
        return dict(t)

    def search(self, addr: str) -> list[str]:
        """Hashes of every tx the archive node indexes for the address (cached; re-run with --refresh)."""
        key = {"addr": addr}
        c = None if self.refresh else self.store.get_raw(self.src, "search", key)
        if c is not None:
            return list(c)
        found: dict[str, None] = {}
        for q in SEARCH_QUERIES:
            page = 1
            while True:
                r = self.get(f"{self.p.archive_rest}/cosmos/tx/v1beta1/txs",
                             {"query": q.format(a=addr), "page": page, "limit": 100, "order_by": "ORDER_BY_ASC"})
                txs, resps = r.get("txs") or [], r.get("tx_responses") or []
                for tx, resp in zip(txs, resps):
                    h = str(resp["txhash"]).upper()
                    found[h] = None
                    if self.store.get_raw(self.src, "tx", {"hash": h}) is None:
                        self.store.put_raw(self.src, "tx", {"hash": h}, {"tx": tx, "tx_response": resp})
                total = int(r.get("total") or (r.get("pagination") or {}).get("total") or 0)
                if len(resps) < 100 or page * 100 >= total:
                    break
                page += 1
        self.store.put_raw(self.src, "search", key, list(found))
        return list(found)

    def history(self, wallet: str, refs: list[TxRef]) -> list[dict[str, Any]]:
        hashes = dict.fromkeys(self.full_hash(r) for r in refs if r.wallet == wallet)
        if self.p.search:
            hashes.update(dict.fromkeys(self.search(wallet)))
        out = [self.tx(h) for h in hashes]
        out.sort(key=lambda t: (int(t["tx_response"]["height"]), t["tx_response"]["txhash"]))
        log.info("  %s %s…: %d txs", self.p.slug, wallet[:12], len(out))
        return out

    def bridge_credits(self, wallet: str, txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """dYdX Ethereum-bridge deposits to the wallet: one credit per acknowledged bridge event, at completion."""
        out = []
        for t in txs:
            for m in t["tx"]["body"].get("messages") or []:
                if not str(m.get("@type", "")).endswith("MsgAcknowledgeBridges"):
                    continue
                for ev in m.get("events") or []:
                    if ev.get("address") != wallet:
                        continue
                    key = {"id": ev["id"]}
                    c = self.store.get_raw(self.src, "bridge_credit", key)
                    if c is None:
                        h = int(t["tx_response"]["height"])
                        sp = self.get(f"{self.p.archive_rest}/dydxprotocol/v4/bridge/safety_params",
                                      headers={"x-cosmos-block-height": str(h)})
                        done = h + int(sp["params"]["delay_blocks"])
                        b = self.get(f"{self.p.archive_rest}/cosmos/base/tendermint/v1beta1/blocks/{done}")
                        c = {"id": ev["id"], "denom": ev["coin"]["denom"], "amount": ev["coin"]["amount"],
                             "eth_block": ev.get("eth_block_height", ""), "ack_tx": t["tx_response"]["txhash"],
                             "height": done, "time": b["block"]["header"]["time"]}
                        self.store.put_raw(self.src, "bridge_credit", key, c)
                    out.append(dict(c))
        return out

    # -- current state ------------------------------------------------------------------------------------
    def module_accounts(self) -> dict[str, str]:
        c = self.store.get_raw(f"{self.p.slug}-state", "module_accounts", {})
        if c is None:
            accs = self.get(f"{self.p.state_rest}/cosmos/auth/v1beta1/module_accounts").get("accounts", [])
            c = {(a.get("base_account") or {}).get("address") or a.get("address"): a.get("name") for a in accs}
            self.store.put_raw(f"{self.p.slug}-state", "module_accounts", {}, c)
        return dict(c)

    def balances(self, addr: str) -> dict[str, int]:
        bal = self.get(f"{self.p.state_rest}/cosmos/bank/v1beta1/balances/{addr}").get("balances") or []
        return {b["denom"]: int(b["amount"]) for b in bal}

    def staked(self, addr: str) -> dict[str, int]:
        """Delegated plus unbonding amounts (they are not in the bank balance)."""
        nd = self.p.native_denom
        out: dict[str, int] = {}
        for d in self.get(f"{self.p.state_rest}/cosmos/staking/v1beta1/delegations/{addr}").get(
                "delegation_responses") or []:
            b = d.get("balance") or {}
            out[b.get("denom", nd)] = out.get(b.get("denom", nd), 0) + int(b.get("amount") or 0)
        for u in self.get(f"{self.p.state_rest}/cosmos/staking/v1beta1/delegators/{addr}/unbonding_delegations").get(
                "unbonding_responses") or []:
            for e in u.get("entries") or []:
                out[nd] = out.get(nd, 0) + int(e.get("balance") or 0)
        return out

    def denom_trace(self, denom: str) -> tuple[str, int]:
        """IBC voucher denom -> (symbol, decimals) from its base denom (uatom -> ATOM, 6). Cached on success only:
        a failed lookup must not pin a wrong decimal count."""
        c = self.store.get_raw(f"{self.p.slug}-state", "denom_meta", {"d": denom})
        if c is not None:
            return str(c[0]), int(c[1])
        base = denom
        if denom.startswith("ibc/"):
            base = ""
            for path, keys in ((f"/ibc/apps/transfer/v1/denoms/{denom[4:]}", ("denom", "base")),  # ibc-go v10
                               (f"/ibc/apps/transfer/v1/denom_traces/{denom[4:]}",  # older ibc-go (dYdX)
                                ("denom_trace", "base_denom"))):
                try:
                    r = httpx.get(self.p.state_rest + path, timeout=20)
                    r.raise_for_status()
                    base = str(r.json()[keys[0]][keys[1]])
                    break
                except Exception:
                    continue
            if not base:
                log.warning("  %s denom %s: metadata lookup failed", self.p.slug, denom[:20])
                return denom[:12], 0
        sym, dec = base_meta(base)
        self.store.put_raw(f"{self.p.slug}-state", "denom_meta", {"d": denom}, [sym, dec])
        return sym, dec


def base_meta(base: str) -> tuple[str, int]:
    """Cosmos base denom convention: u = micro (6 decimals), a = atto (18); factory/…/name -> name. Stride
    liquid-staking tokens prefix 'st' (stuatom = stATOM, 6; stinj / staevmos = 18 like their base)."""
    if base.startswith("factory/"):
        return base.rsplit("/", 1)[-1][:12].upper(), 6
    if base.startswith("st") and len(base) > 3 and base != "stake":
        sym, dec = base_meta(base[2:])
        if base[2:] == "inj":
            dec = 18
        return "st" + sym, dec
    if base.startswith("u") and base[1:].isalpha():
        return base[1:].upper(), 6
    if base.startswith("a") and base[1:].isalpha() and len(base) > 3:
        return base[1:].upper(), 18
    return base[:12].upper(), 0
