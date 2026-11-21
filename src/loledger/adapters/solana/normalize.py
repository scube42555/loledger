"""Solana tx -> wallet-perspective Movements from the tx's own balance deltas (exact).

SOL: (post - pre) lamports of the wallet's account, plus the fee when the wallet paid it (the fee
is gas, not a transfer). Tokens: post - pre of token accounts whose owner is the wallet, per mint.
Counterparty: the other party of a matching parsed transfer instruction if there is one, else the
tx's main program. meta.err -> failed tx (fee only).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale

PLUMBING = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}
WSOL = "So11111111111111111111111111111111111111112"


# programs that pay validator-shared rewards into stake accounts (MEV tips, block rewards)
REWARD_DISTRIBUTORS = {
    "4R3gSG8BpU4t19KYj8CfnbtRpnT8gtk4dvTHxVRwc2r7": "Jito tip distribution (MEV rewards)",
    "RouterBmuRBkPUbgEDMtdvTZ75GBdSREZR5uGUxxxpb": "block-reward router (validator-shared block rewards)",
}


def _keys(tx: dict[str, Any]) -> list[str]:
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    ins = list(tx["transaction"]["message"].get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ins.extend(inner.get("instructions") or [])
    return ins


def main_program(tx: dict[str, Any]) -> str:
    for ix in tx["transaction"]["message"].get("instructions") or []:
        pid = ix.get("programId")
        if pid and pid not in PLUMBING:
            return str(pid)
    for ix in tx["transaction"]["message"].get("instructions") or []:
        if ix.get("programId"):
            return str(ix["programId"])
    return ""


def _others_moved(meta: dict[str, Any], wallet: str) -> bool:
    """True when the tx moved tokens for some owner other than the fee-paying wallet."""
    owners = {b.get("owner") for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])}
    return bool(owners - {wallet, None})


def normalize(chain: ChainConfig, wallet: str, history: list[dict[str, Any]], symbols: dict[str, str]
              ) -> tuple[list[Movement], dict[str, TxContext]]:
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for tx in history:
        meta = tx.get("meta") or {}
        sig = tx["transaction"]["signatures"][0]
        keys = _keys(tx)
        ts = int(tx.get("blockTime") or 0)
        slot = int(tx.get("slot") or 0)
        fee_payer = keys[0] if keys else ""
        fee = int(meta.get("fee") or 0)
        err = meta.get("err") is not None
        prog = main_program(tx)
        programs = sorted({str(ix.get("programId")) for ix in _instructions(tx) if ix.get("programId")} - PLUMBING)
        # counterparties from parsed transfers involving the wallet (or its token accounts)
        wallet_accounts = {wallet} | {keys[b["accountIndex"]] for b in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or [])
                                      if b.get("owner") == wallet}
        cp_by_asset: dict[str, str] = {}
        for ix in _instructions(tx):
            p = ix.get("parsed")
            if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked", "createAccount",
                                                                 "createAccountWithSeed", "withdraw", "split"):
                continue
            info = p.get("info") or {}
            if p.get("type") == "withdraw" and ix.get("program") != "stake":
                continue
            src = info.get("stakeAccount") if p.get("type") in ("withdraw", "split") else \
                info.get("source") or info.get("authority")
            dst = info.get("newAccount") or info.get("newSplitAccount") or info.get("destination")
            asset = NATIVE_ASSET if ix.get("program") in ("system", "stake") else (info.get("mint") or "token")
            if src in wallet_accounts and dst not in wallet_accounts:
                cp_by_asset.setdefault(asset, str(dst))
            elif dst in wallet_accounts and src not in wallet_accounts:
                cp_by_asset.setdefault(asset, str(info.get("authority") or src))
        legs: list[Movement] = []

        def leg(asset: str, raw: int, dec: int, sym: str, kind: AssetKind) -> None:
            if raw == 0:
                return
            cp = cp_by_asset.get(asset) or cp_by_asset.get("token") or prog
            reward = asset == NATIVE_ASSET and raw > 0 and (cp in REWARD_DISTRIBUTORS or prog in REWARD_DISTRIBUTORS)
            legs.append(Movement(chain=chain.name, tx_hash=sig, leg_id=f"sol:{asset[:12]}", block=slot, timestamp=ts,
                                 wallet=wallet, asset_id=asset, asset_kind=kind, symbol=sym, decimals=dec,
                                 amount=scale(raw, dec), counterparty=cp,
                                 source="staking_reward" if reward else "solana"))

        if not err:
            if wallet in keys:
                i = keys.index(wallet)
                delta = int(meta["postBalances"][i]) - int(meta["preBalances"][i])
                if wallet == fee_payer:
                    delta += fee
                leg(NATIVE_ASSET, delta, 9, chain.native_symbol, AssetKind.NATIVE)
            per_mint: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for side, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
                for b in meta.get(key) or []:
                    if b.get("owner") != wallet:
                        continue
                    ua = b["uiTokenAmount"]
                    per_mint[b["mint"]][0] += side * int(ua["amount"])
                    per_mint[b["mint"]][1] = int(ua["decimals"])
            for mint, (raw, dec) in per_mint.items():
                sym = "wSOL" if mint == WSOL else symbols.get(mint, f"{mint[:4]}…{mint[-4:]}")
                leg(mint, raw, dec, sym, AssetKind.ERC20)  # SPL tokens reuse the fungible-token kind
        moves.extend(legs)
        if legs or wallet == fee_payer:
            ix_accounts = {str(ix.get("programId")): [str(a) for a in ix.get("accounts") or []]
                           for ix in tx["transaction"]["message"].get("instructions") or []
                           if ix.get("programId") and ix.get("programId") not in PLUMBING and ix.get("accounts")}
            fn = ",".join(programs[:3]) if legs or not _others_moved(meta, wallet) else "fee_payer_only"
            txs[sig] = TxContext(chain=chain.name, tx_hash=sig, block=slot, timestamp=ts, tx_from=fee_payer, tx_to=prog,
                                 method_id="", function_name=fn, is_error=err, gas_fee=scale(fee, 9), value=Decimal(0),
                                 meta={"programs": programs, "ix_accounts": ix_accounts,
                                       "stake_ix": _stake_ix(tx)})
    return moves, txs


def _stake_ix(tx: dict[str, Any]) -> list[dict[str, str]]:
    """Parsed stake-program instructions: type plus the stake account and the destination of a withdraw."""
    out = []
    for ix in tx["transaction"]["message"].get("instructions") or []:
        p = ix.get("parsed")
        if ix.get("program") == "stake" and isinstance(p, dict):
            info = p.get("info") or {}
            out.append({"type": str(p.get("type")), "stake": str(info.get("stakeAccount", "")),
                        "destination": str(info.get("destination", ""))})
    return out


STAKE_REWARD_CP = "solana:inflation_rewards"


def stake_reward_movements(chain: ChainConfig, account: str, rewards: list[tuple[int, int, int]]
                           ) -> tuple[list[Movement], dict[str, TxContext]]:
    """(epoch, lamports, block time) per epoch -> one income leg each; credited outside any transaction."""
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for epoch, lamports, ts in rewards:
        h = f"sol-reward:{epoch}:{account}"
        moves.append(Movement(chain=chain.name, tx_hash=h, leg_id="sol:reward", block=0, timestamp=ts, wallet=account,
                              asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol, decimals=9,
                              amount=scale(lamports, 9), counterparty=STAKE_REWARD_CP, source="staking_reward"))
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=0, timestamp=ts, tx_from="", tx_to=account, method_id="",
                           function_name="epoch_reward", is_error=False, gas_fee=Decimal(0), value=Decimal(0),
                           meta={"epoch": epoch})
    return moves, txs
