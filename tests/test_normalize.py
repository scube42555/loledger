from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from loledger.adapters.evm.fetch import EvmRawBundle
from loledger.adapters.evm.normalize import balances, normalize
from loledger.config import ConfigError, load_settings, load_wallets

from .fakes import CHAIN, OTHER, ROUTER, TOKEN, W, token_row, transfer_log, tx_row

E18 = 10**18


def _bundle(**rows: list[dict[str, Any]]) -> EvmRawBundle:
    full: dict[str, list[dict[str, Any]]] = {a: [] for a in ("txlist", "txlistinternal", "tokentx", "tokennfttx", "token1155tx")}
    full.update(rows)
    return EvmRawBundle(wallet=W, rows=full)


def test_native_send_and_gas() -> None:
    b = _bundle(txlist=[tx_row("0x01", 10, W, OTHER, 2 * E18)])
    moves, txs = normalize(CHAIN, b)
    assert [(m.asset_id, m.amount, m.counterparty) for m in moves] == [("native", Decimal(-2), OTHER)]
    assert txs["0x01"].gas_fee == Decimal(21000 * 25 * 10**9).scaleb(-18)
    assert balances(moves, txs, W)["native"] == Decimal(-2) - txs["0x01"].gas_fee


def test_failed_tx_moves_no_value_but_pays_gas() -> None:
    b = _bundle(txlist=[tx_row("0x02", 11, W, ROUTER, E18, is_error="1")])
    moves, txs = normalize(CHAIN, b)
    assert moves == []
    assert txs["0x02"].is_error
    assert balances(moves, txs, W)["native"] == -txs["0x02"].gas_fee


def test_self_transfer_gets_two_legs_that_net_to_zero() -> None:
    b = _bundle(txlist=[tx_row("0x03", 12, W, W, E18)])
    moves, _ = normalize(CHAIN, b)
    assert sorted(m.leg_id for m in moves) == ["value:in", "value:out"]
    assert sum(m.amount for m in moves) == 0


def test_token_legs_get_log_index_from_receipt_and_duplicates_survive() -> None:
    h = "0x04"
    b = _bundle(tokentx=[token_row(h, 13, OTHER, W, 1_500_000), token_row(h, 13, OTHER, W, 1_500_000)])
    b.receipts = {h: {"from": OTHER, "to": ROUTER, "status": "0x1", "gasUsed": "0x5208",
                      "effectiveGasPrice": "0x1", "logs": [transfer_log(TOKEN, OTHER, W, 1_500_000, 7),
                                                           transfer_log(TOKEN, OTHER, W, 1_500_000, 9)]}}
    b.txs = {h: {"from": OTHER, "to": ROUTER, "value": "0x0", "input": "0x12345678abcd"}}
    moves, txs = normalize(CHAIN, b)
    assert [m.leg_id for m in moves] == ["log:7", "log:9"]
    assert all(m.amount == Decimal("1.5") and m.symbol == "USDC" for m in moves)
    # tx not sent by the wallet: context comes from receipt/tx, and gas is not charged to the wallet
    assert (txs[h].tx_from, txs[h].tx_to, txs[h].method_id) == (OTHER, ROUTER, "0x12345678")
    assert "native" not in balances(moves, txs, W)


def test_internal_native_leg_and_errored_trace_skipped() -> None:
    b = _bundle(txlistinternal=[
        {"blockNumber": "14", "timeStamp": "1", "hash": "0x05", "from": ROUTER, "to": W, "value": str(E18),
         "traceId": "0_1", "isError": "0"},
        {"blockNumber": "14", "timeStamp": "1", "hash": "0x05", "from": ROUTER, "to": W, "value": str(E18),
         "traceId": "0_2", "isError": "1"},
    ])
    moves, _ = normalize(CHAIN, b)
    assert [(m.leg_id, m.amount, m.source) for m in moves] == [("internal:0_1", Decimal(1), "internal")]


def test_nft_and_erc1155_assets_keyed_by_token_id() -> None:
    nft = {"blockNumber": "15", "timeStamp": "1", "hash": "0x06", "from": "0x" + "00" * 20, "to": W,
           "contractAddress": ROUTER, "tokenID": "42", "tokenSymbol": "UNI-V3-POS"}
    lb = {"blockNumber": "15", "timeStamp": "1", "hash": "0x06", "from": "0x" + "00" * 20, "to": W,
          "contractAddress": TOKEN, "tokenID": "8388608", "tokenValue": "1000", "tokenSymbol": "LBT"}
    moves, _ = normalize(CHAIN, _bundle(tokennfttx=[nft], token1155tx=[lb]))
    assert {(m.asset_id, m.amount) for m in moves} == {(f"{ROUTER}:42", Decimal(1)), (f"{TOKEN}:8388608", Decimal(1000))}


def test_load_wallets_reads_chain_column(tmp_path: Path) -> None:
    p = tmp_path / "a.csv"
    p.write_text("Address,Chain\n0xABCDEF0000000000000000000000000000000001,Avalanche\n")
    [w] = load_wallets(p, load_settings())
    assert (w.address, w.chain.chain_id) == ("0xabcdef0000000000000000000000000000000001", 43114)
    p.write_text("Address,Chain\nnot-an-address,Avalanche\n")
    with pytest.raises(ConfigError):
        load_wallets(p, load_settings())


def test_load_wallets_accepts_cardano_rows(tmp_path: Path) -> None:
    p = tmp_path / "a.csv"
    p.write_text("Address,Chain\nstake1u9xyz,Cardano\naddr1qxyz,cardano\n")
    ws = load_wallets(p, load_settings())
    assert [(w.chain.kind, w.address) for w in ws] == [("cardano", "stake1u9xyz"), ("cardano", "addr1qxyz")]


def test_wavax_deposit_and_withdrawal_become_legs() -> None:
    wavax = CHAIN.wrapped_native
    assert wavax
    pad = "0x" + W[2:].rjust(64, "0")
    dep = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
    wd = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
    b = _bundle(txlist=[tx_row("0x07", 16, W, wavax, 2 * E18), tx_row("0x08", 17, W, wavax)],
                txlistinternal=[{"blockNumber": "17", "timeStamp": "1", "hash": "0x08", "from": wavax, "to": W,
                                 "value": str(E18), "traceId": "0", "isError": "0"}])
    b.receipts = {
        "0x07": {"logs": [{"address": wavax, "topics": [dep, pad], "data": hex(2 * E18), "logIndex": "0x0"}]},
        "0x08": {"logs": [{"address": wavax, "topics": [wd, pad], "data": hex(E18), "logIndex": "0x1"}]},
    }
    moves, txs = normalize(CHAIN, b)
    bal = balances(moves, txs, W)
    assert bal[wavax] == Decimal(1)
    assert [m.source for m in moves if m.asset_id == wavax] == ["wrap", "wrap"]
    assert bal["native"] == Decimal(-1) - txs["0x07"].gas_fee - txs["0x08"].gas_fee


def test_atomic_changes_become_movements_with_tx_context() -> None:
    from loledger.adapters.evm.atomic import AtomicChange
    b = _bundle()
    b.atomic = [AtomicChange(50, Decimal("-25.2393369"))]
    b.block_ts = {50: 1_710_000_000}
    moves, txs = normalize(CHAIN, b)
    [m] = moves
    assert (m.tx_hash, m.source, m.amount, m.timestamp) == ("atomic:50", "atomic", Decimal("-25.2393369"), 1_710_000_000)
    assert txs["atomic:50"].function_name == "atomic_export"


def test_unsupported_chain_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    p = tmp_path / "a.csv"
    p.write_text("Address,Chain\ntz1example,Tezos\n0xABCDEF0000000000000000000000000000000001,Avalanche\n")
    ws = load_wallets(p, load_settings())
    assert [w.chain.name for w in ws] == ["Avalanche"]


def test_beacon_withdrawals_become_native_legs() -> None:
    b = _bundle(beacon=[{"withdrawalIndex": "7", "validatorIndex": "1095143", "address": W,
                         "amount": "17531074", "blockNumber": "23984546", "timestamp": "1765000000"}])
    moves, txs = normalize(CHAIN, b)
    [m] = moves
    assert (m.tx_hash, m.amount, m.source) == ("beacon:23984546", Decimal("0.017531074"), "beacon")
    assert txs["beacon:23984546"].meta["validators"] == ["1095143"]


def test_proposer_rewards_become_native_legs() -> None:
    b = _bundle(proposer=[{"blockNumber": "22046835", "timeStamp": "1741976051", "blockReward": "7097127746075986"}])
    moves, txs = normalize(CHAIN, b)
    [m] = moves
    assert (m.tx_hash, m.amount, m.source) == ("block:22046835", Decimal("0.007097127746075986"), "proposer")
    assert txs["block:22046835"].function_name == "proposer_reward"
