"""Solana normalization from balance deltas (hand-built jsonParsed transactions)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from txclass.adapters.solana.normalize import main_program, normalize
from txclass.config import load_settings

SOL = load_settings().chains["Solana"]
W, X = "Wa11et1111111111111111111111111111111111111", "Other11111111111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def tx(sig: str, keys: list[str], pre: list[int], post: list[int], fee: int = 5000, *, programs: list[str] | None = None,
       pre_tok: list[dict[str, Any]] | None = None, post_tok: list[dict[str, Any]] | None = None, err: Any = None) -> dict[str, Any]:
    return {"slot": 1, "blockTime": 1_700_000_000,
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": k} for k in keys],
                                                               "instructions": [{"programId": p, "accounts": []} for p in programs or []]}},
            "meta": {"fee": fee, "err": err, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": pre_tok or [], "postTokenBalances": post_tok or [], "innerInstructions": []}}


def tok(idx: int, owner: str, mint: str, raw: int, dec: int = 6) -> dict[str, Any]:
    return {"accountIndex": idx, "owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(raw), "decimals": dec}}


def test_sol_send_excludes_fee_and_swap_has_both_legs() -> None:
    send = tx("s1", [W, X], [10_000_000_000, 0], [8_999_995_000, 1_000_000_000], programs=["11111111111111111111111111111111"])
    moves, txs = normalize(SOL, W, [send], {})
    [m] = moves
    assert m.amount == Decimal("-1") and txs["s1"].gas_fee == Decimal("0.000005")
    swap = tx("s2", [W, "WATA111111111111111111111111111111111111111"], [2_000_000_000, 0], [999_995_000, 0],
              programs=["ComputeBudget111111111111111111111111111111", JUP],
              pre_tok=[tok(1, W, USDC, 0)], post_tok=[tok(1, W, USDC, 150_000_000)])
    moves, txs = normalize(SOL, W, [swap], {USDC: "USDC"})
    assert {(m.symbol, m.amount) for m in moves} == {("SOL", Decimal("-1")), ("USDC", Decimal("150"))}
    assert main_program(swap) == JUP and txs["s2"].tx_to == JUP


def test_received_tokens_and_failed_tx() -> None:
    recv = tx("r1", [X, "WATA111111111111111111111111111111111111111"], [5_000_000_000, 2_039_280], [4_999_995_000, 2_039_280],
              pre_tok=[tok(1, W, USDC, 0)], post_tok=[tok(1, W, USDC, 5_000_000)])
    moves, txs = normalize(SOL, W, [recv], {USDC: "USDC"})
    assert [(m.symbol, m.amount) for m in moves] == [("USDC", Decimal("5"))] and txs["r1"].tx_from == X
    fail = tx("f1", [W], [1_000_000, ], [995_000], err={"InstructionError": [0, "Custom"]})
    moves, txs = normalize(SOL, W, [fail], {})
    assert moves == [] and txs["f1"].is_error and txs["f1"].gas_fee == Decimal("0.000005")


def test_stake_account_discovery_and_epoch_rewards() -> None:
    from decimal import Decimal

    from txclass.adapters.solana.fetch import STAKE_PROGRAM, stake_accounts
    from txclass.adapters.solana.normalize import stake_reward_movements
    from txclass.config import load_settings
    from txclass.rules import RuleContext, classify_group
    from txclass.registry import load_registry

    w, acct = "Wa11et1111111111111111111111111111111111111", "StakeAcct111111111111111111111111111111111"
    tx = {"meta": {}, "transaction": {"message": {"instructions": [
        {"program": "system", "parsed": {"type": "createAccount",
                                         "info": {"source": w, "newAccount": acct, "owner": STAKE_PROGRAM}}},
        {"program": "stake", "parsed": {"type": "delegate", "info": {"stakeAccount": acct, "stakeAuthority": w}}},
        {"program": "stake", "parsed": {"type": "delegate", "info": {"stakeAccount": "NotMine", "stakeAuthority": "x"}}},
    ]}}}
    assert stake_accounts(w, [tx]) == [acct]
    chain = load_settings().chains["Solana"]
    moves, txs = stake_reward_movements(chain, acct, [(600, 157_396_733, 1_700_000_000)])
    assert moves[0].amount == Decimal("0.157396733") and moves[0].wallet == acct
    ctx = RuleContext(chain=chain, wallets={w, acct}, registry=load_registry("Solana", {}))
    rows = classify_group(acct, txs[moves[0].tx_hash], moves, ctx)
    assert [(r.classification.value, r.subtype) for r in rows] == [("OTHER", "staking_reward")]
