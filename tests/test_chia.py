"""Chia: coin ids, address encoding, and single-address normalization incl. bundled spends."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from txclass.adapters.chia.fetch import address_to_puzzle_hash, coin_id, puzzle_hash_to_address
from txclass.adapters.chia.normalize import normalize
from txclass.config import load_settings

CHIA = load_settings().chains["Chia"]
PH = "0x" + "11" * 32
OTHER = "0x" + "22" * 32
SIB = "0x" + "33" * 32


def test_address_round_trip_and_coin_id_encoding() -> None:
    a = puzzle_hash_to_address(PH)
    assert a.startswith("xch1") and address_to_puzzle_hash(a) == PH
    assert coin_id("0x" + "00" * 32, PH, 0) != coin_id("0x" + "00" * 32, PH, 1)


class FakeChia:
    def __init__(self, additions: dict[int, list[dict[str, Any]]]) -> None:
        self.additions = additions

    def block(self, h: int) -> dict[str, Any]:
        return {"timestamp": 1000 + h, "header_hash": f"hh{h}"}

    def additions_removals(self, hh: str) -> dict[str, Any]:
        return {"additions": self.additions[int(hh[2:])]}

    def coin(self, name: str) -> dict[str, Any]:
        return {"coin": {"puzzle_hash": OTHER}}


def rec(parent: str, ph: str, amt: int, height: int, spent_at: int | None = None, coinbase: bool = False) -> dict[str, Any]:
    return {"coin": {"parent_coin_info": parent, "puzzle_hash": ph, "amount": amt}, "confirmed_block_index": height,
            "spent_block_index": spent_at or 0, "spent": spent_at is not None, "coinbase": coinbase, "timestamp": 1000 + height}


def test_receive_spend_with_change_and_bundle_inputs_reconcile() -> None:
    got = rec("0x" + "aa" * 32, PH, 5_000, 10, spent_at=20)
    farm = rec("0x" + "bb" * 32, PH, 250, 11, coinbase=True)
    mine = coin_id(got["coin"]["parent_coin_info"], PH, 5_000)
    # spend at 20: our 5,000 coin carries all outputs; a sibling address added 3,000; 7,900 out, 100 change
    adds = {20: [{"coin": {"parent_coin_info": mine, "puzzle_hash": OTHER, "amount": 7_900}},
                 {"coin": {"parent_coin_info": mine, "puzzle_hash": PH, "amount": 100}}]}
    change = rec(mine, PH, 100, 20)
    w = puzzle_hash_to_address(PH)
    moves, txs = normalize(CHIA, w, PH, [got, farm, change], FakeChia(adds))  # type: ignore[arg-type]
    total = sum((m.amount for m in moves), Decimal(0)) - sum((t.gas_fee for t in txs.values() if t.tx_from == w), Decimal(0))
    assert total == Decimal(100 + 250).scaleb(-12)  # unspent: change + farming reward
    assert {m.source for m in moves} == {"chia", "farming_reward"}
    assert any(m.counterparty == "chia:other_inputs_in_same_spend" and m.amount == Decimal(3_000).scaleb(-12) for m in moves)


def test_pooled_spend_across_own_addresses_becomes_internal() -> None:
    from decimal import Decimal

    from txclass.adapters.chia.normalize import BUNDLE_CP, pair_bundles
    from txclass.model import NATIVE_ASSET, AssetKind, Movement, TxContext

    def mv(w: str, leg: str, amt: str) -> Movement:
        return Movement(chain="Chia", tx_hash=f"chia:7:{w}", leg_id=leg, block=7, timestamp=1, wallet=w,
                        asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol="XCH", decimals=12,
                        amount=Decimal(amt), counterparty=BUNDLE_CP, source="chia")

    def tx(w: str) -> TxContext:
        return TxContext(chain="Chia", tx_hash=f"chia:7:{w}", block=7, timestamp=1, tx_from=w, tx_to=w, method_id="",
                         function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0))

    res = {"cold": ([mv("cold", "bundle_contribution", "-5")], {"chia:7:cold": tx("cold")}),
           "pool": ([mv("pool", "bundle_inputs", "5.8"), mv("pool", "coin:x", "-34")], {"chia:7:pool": tx("pool")})}
    notes = pair_bundles(res)
    assert len(notes) == 1
    assert res["cold"][0][0].counterparty == "pool"
    pool = {(m.counterparty, m.amount) for m in res["pool"][0]}
    assert pool == {("cold", Decimal(5)), (BUNDLE_CP, Decimal("0.8")), (BUNDLE_CP, Decimal(-34))}
