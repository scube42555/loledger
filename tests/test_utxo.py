"""UTXO normalization: change, fees, multi-wallet txs, exact vout linking."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from txclass.adapters.utxo.normalize import normalize
from txclass.config import load_settings

DOGE = load_settings().chains["Doge"]
A, B, C = "DAaaa", "DBbbb", "DCccc"


def _tx(txid: str, ts: int, ins: list[tuple[str, int]], outs: list[tuple[str, int]], fee: int) -> dict[str, Any]:
    return {"txid": txid, "ts": ts, "height": ts, "fee": fee, "inputs": [{"addr": a, "value": v} for a, v in ins],
            "outputs": [{"addr": a, "value": v, "n": n} for n, (a, v) in enumerate(outs)]}


def test_send_with_change_reconciles_and_fee_is_gas() -> None:
    t = _tx("t1", 1, [(A, 1000)], [(B, 300), (A, 690)], 10)
    moves, txs = normalize(DOGE, A, [t])
    [m] = moves
    assert (m.amount, m.counterparty, m.leg_id) == (Decimal(-300).scaleb(-8), B, "vout:0")
    assert txs["t1"].tx_from == A and txs["t1"].gas_fee == Decimal(10).scaleb(-8)


def test_shared_tx_context_is_the_same_from_every_wallet() -> None:
    t = _tx("t2", 1, [(A, 5077)], [(B, 5), (C, 5071)], 1)
    _, from_a = normalize(DOGE, A, [t])
    moves_b, from_b = normalize(DOGE, B, [t])
    assert from_a["t2"].gas_fee == from_b["t2"].gas_fee == Decimal(1).scaleb(-8)
    assert from_b["t2"].tx_from == A and [m.amount for m in moves_b] == [Decimal(5).scaleb(-8)]


def test_coinjoin_style_shared_inputs_get_one_net_leg() -> None:
    t = _tx("t3", 1, [(A, 100), (B, 100)], [(C, 150), (A, 40)], 10)
    moves, txs = normalize(DOGE, A, [t])
    assert [m.amount for m in moves] == [Decimal(-60).scaleb(-8)] and txs["t3"].gas_fee == 0


def test_doge_and_kaspa_lookalikes_are_poisoning() -> None:
    from txclass.rules import _lookalike
    w = "DTest" + "Q" * 25 + "5qC6"  # synthetic 34-char Doge-style addresses
    doge_wallets = {w}
    assert _lookalike("DTest" + "K" * 25 + "5qC6", doge_wallets) == w
    assert _lookalike("DXyzQ" + "Q" * 25 + "zzzz", doge_wallets) is None  # only the 'D' is shared
    kas = {"kaspa:qypupccf" + "m" * 50 + "rxj6s"}
    assert _lookalike("kaspa:qypuzzzz" + "m" * 50 + "rxj6s", kas)
    assert _lookalike("cardano:rewards", {"cardano:deposit"}) is None
    assert _lookalike("stake1ux" + "p" * 32 + "su9s", {"stake1uq" + "z" * 32 + "86aa"}, prefix_only=True) is None
    w1, look = "DFvt8" + "w" * 29, "DFvt8" + "g" * 28 + "K"
    assert _lookalike(look, {w1}) is None
    assert _lookalike(look, {w1}, prefix_only=True)
