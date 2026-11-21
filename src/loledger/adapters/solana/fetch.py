"""Solana via public JSON-RPC: getSignaturesForAddress (paged) + getTransaction (jsonParsed, cached)."""

from __future__ import annotations

import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient, TransientError
from ...store import Store

log = logging.getLogger(__name__)


def _check(body: Any) -> None:
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        if isinstance(err, dict) and err.get("code") in (429, -32005, -32429) or "limit" in str(err).lower():
            raise TransientError(str(err))
        raise RuntimeError(f"Solana RPC error: {err}")


class SolanaClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None) -> None:
        if not chain.rpc_url:
            raise ValueError("Solana needs rpc_url")
        self.url = chain.rpc_url
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 2)

    def rpc(self, method: str, params: list[Any]) -> Any:
        body = self.http.request_json("POST", self.url, json_body={"jsonrpc": "2.0", "id": 1, "method": method,
                                                                    "params": params}, check=_check)
        return body.get("result")

    def signatures(self, addr: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        before: str | None = None
        while True:
            opts: dict[str, Any] = {"limit": 1000, "commitment": "finalized"}
            if before:
                opts["before"] = before
            page = self.rpc("getSignaturesForAddress", [addr, opts]) or []
            out.extend(page)
            if len(page) < 1000:
                return out
            before = page[-1]["signature"]

    def transaction(self, sig: str) -> dict[str, Any] | None:
        c = self.store.get_raw("solana-rpc", "getTransaction", {"sig": sig})
        if c is not None:
            return dict(c)
        tx = self.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                               "commitment": "finalized"}])
        if tx is not None:
            self.store.put_raw("solana-rpc", "getTransaction", {"sig": sig}, tx)
        return dict(tx) if tx else None

    def token_accounts(self, addr: str) -> set[str]:
        out: set[str] = set()
        for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            res = self.rpc("getTokenAccountsByOwner", [addr, {"programId": program}, {"encoding": "jsonParsed"}]) or {}
            out |= {str(a["pubkey"]) for a in res.get("value", [])}
        return out

    def history(self, addr: str) -> list[dict[str, Any]]:
        """Txs touching the wallet *or any of its token accounts*: incoming SPL transfers only name the
        recipient token account, so they are missing from the owner's own signature list."""
        txs: dict[str, dict[str, Any]] = {}
        scanned: set[str] = set()
        todo = {addr} | self.token_accounts(addr)
        missing = 0
        while todo:
            acct = todo.pop()
            scanned.add(acct)
            for s in self.signatures(acct):
                sig = s["signature"]
                if sig in txs:
                    continue
                tx = self.transaction(sig)
                if tx is None:
                    missing += 1
                    continue
                txs[sig] = tx
                keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
                meta = tx.get("meta") or {}
                for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
                    if b.get("owner") == addr:
                        ta = keys[b["accountIndex"]]
                        if ta not in scanned:
                            todo.add(ta)  # closed token accounts are only discoverable from history
        if missing:
            log.warning("  solana %s…: %d transaction(s) unavailable from the RPC", addr[:8], missing)
        log.info("  solana %s…: %d txs over %d accounts (wallet + token accounts)", addr[:8], len(txs), len(scanned))
        return sorted(txs.values(), key=lambda t: (t.get("blockTime") or 0, t.get("slot") or 0))

    # ---- native staking: stake accounts the wallet controls, and their per-epoch inflation rewards

    def current_epoch(self) -> int:
        return int((self.rpc("getEpochInfo", []) or {}).get("epoch", 0))

    def inflation_rewards(self, accounts: list[str], epoch: int, final: bool) -> list[dict[str, Any] | None]:
        """Rewards credited for `epoch` (paid at the start of epoch+1). Completed epochs are cached."""
        key = {"epoch": epoch, "accounts": sorted(accounts)}
        c = self.store.get_raw("solana-rpc", "getInflationReward", key) if final else None
        if c is None:
            c = self.rpc("getInflationReward", [sorted(accounts), {"epoch": epoch, "commitment": "finalized"}]) or []
            if final:
                self.store.put_raw("solana-rpc", "getInflationReward", key, c)
        return list(c)

    def block_time(self, slot: int) -> int:
        c = self.store.get_raw("solana-rpc", "getBlockTime", {"slot": slot})
        if c is None:
            c = self.rpc("getBlockTime", [slot])
            self.store.put_raw("solana-rpc", "getBlockTime", {"slot": slot}, c)
        return int(c or 0)

    def balance(self, addr: str) -> int:
        return int((self.rpc("getBalance", [addr, {"commitment": "finalized"}]) or {}).get("value", 0))

    def token_balances(self, addr: str) -> dict[str, tuple[int, int]]:
        """mint -> (raw amount, decimals) summed over the owner's token accounts (both token programs)."""
        out: dict[str, tuple[int, int]] = {}
        for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            res = self.rpc("getTokenAccountsByOwner", [addr, {"programId": program}, {"encoding": "jsonParsed"}]) or {}
            for acc in res.get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                mint, ta = info["mint"], info["tokenAmount"]
                prev = out.get(mint, (0, int(ta["decimals"])))
                out[mint] = (prev[0] + int(ta["amount"]), int(ta["decimals"]))
        return out


def resolve_symbols(store: Store, mints: set[str], http: HttpClient | None = None) -> dict[str, str]:
    """Mint -> symbol via DefiLlama current prices (cached; unknown mints keep a short mint id)."""
    http = http or HttpClient(2)
    out: dict[str, str] = {}
    todo = []
    for m in mints:
        c = store.get_raw("defillama", "solana_symbol", {"mint": m})
        if c is not None:
            out[m] = str(c)
        else:
            todo.append(m)
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        body = http.request_json("GET", "https://coins.llama.fi/prices/current/" + ",".join(f"solana:{m}" for m in chunk))
        got = (body or {}).get("coins", {})
        for m in chunk:
            sym = (got.get(f"solana:{m}") or {}).get("symbol") or f"{m[:4]}…{m[-4:]}"
            store.put_raw("defillama", "solana_symbol", {"mint": m}, sym)
            out[m] = sym
    return out


STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"


def stake_accounts(wallet: str, history: list[dict[str, Any]]) -> list[str]:
    """Stake accounts this wallet created or holds stake/withdraw authority over, from its own txs (closed
    accounts included, so their past rewards can be booked)."""
    found: set[str] = set()
    for tx in history:
        meta = tx.get("meta") or {}
        ixs = list(tx["transaction"]["message"].get("instructions") or []) + [
            i for g in meta.get("innerInstructions") or [] for i in g.get("instructions") or []]
        for ix in ixs:
            p = ix.get("parsed")
            if not isinstance(p, dict):
                continue
            info = p.get("info") or {}
            if p.get("type") in ("createAccount", "createAccountWithSeed") and info.get("owner") == STAKE_PROGRAM \
                    and info.get("source") == wallet:
                found.add(str(info["newAccount"]))
            elif ix.get("program") == "stake" or ix.get("programId") == STAKE_PROGRAM:
                auth = {info.get(k) for k in ("stakeAuthority", "withdrawAuthority", "authority", "source")}
                auth |= set((info.get("authorized") or {}).values())
                for k in ("stakeAccount", "newSplitAccount", "destination"):
                    a = info.get(k)
                    if a and wallet in auth and a != wallet and (k != "destination" or p.get("type") == "merge"):
                        found.add(str(a))
    return sorted(found)
