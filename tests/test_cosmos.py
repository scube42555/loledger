"""Cosmos Hub adapter: tx-list parsing, both event layouts (per-message logs / flat msg_index events), fees,
staking rewards and unbonding, IBC receipts, spam memos, denom decimals."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from loledger.adapters.cosmos.fetch import TxRef, base_meta, read_tx_list
from loledger.adapters.cosmos.normalize import normalize
from loledger.config import load_settings

CH = load_settings(None).chains["Cosmos"]
W = "cosmos1wallet"
OTHER = "cosmos1other"
BONDED, DISTR, ESCROW, FEEMKT = "cosmos1bonded", "cosmos1distr", "cosmos1escrow", "cosmos1feemarket"
MODULES = {BONDED: "bonded_tokens_pool", DISTR: "distribution"}
D = Decimal


def _ev(typ: str, idx: int | None = None, **attrs: str) -> dict[str, Any]:
    a = [{"key": k, "value": v} for k, v in attrs.items()]
    if idx is not None:
        a.append({"key": "msg_index", "value": str(idx)})
    return {"type": typ, "attributes": a}


def _tx(h: str, height: int, ts: str, msgs: list[dict[str, Any]], fee: int, *, logs: list[Any] | None = None,
        events: list[Any] | None = None, memo: str = "") -> dict[str, Any]:
    return {"tx": {"body": {"messages": msgs, "memo": memo},
                   "auth_info": {"fee": {"amount": [{"denom": "uatom", "amount": str(fee)}], "payer": ""}}},
            "tx_response": {"txhash": h, "height": str(height), "timestamp": ts, "code": 0,
                            "logs": logs or [], "events": events or []}}


def _meta(denom: str) -> tuple[str, int]:
    return ("NTRN", 6) if denom == "ibc/NTRN" else (denom, 0)


def test_tx_list_and_shortened_hash_matching(tmp_path: Path) -> None:
    p = tmp_path / "txs.csv"
    p.write_text("Address,Height,Hash,Explorer\ncosmos1a,23 863 563,75EFBE5E...A176736671,Send 248 ATOM\n"
                 "cosmos1a,,X...Y,skipped (no height)\n")
    (r,) = read_tx_list(p)
    assert (r.wallet, r.height, r.note) == ("cosmos1a", 23863563, "Send 248 ATOM")
    assert r.matches("75EFBE5E" + "0" * 46 + "A176736671") and not r.matches("75EFBE5F" + "0" * 46 + "A176736671")
    assert TxRef("w", 1, "abcd").matches("ABCD")


def test_old_layout_delegate_new_layout_send_fees_and_internal_counterparty() -> None:
    delegate = _tx("D1", 100, "2024-04-12T16:36:09Z",
                   [{"@type": "/cosmos.staking.v1beta1.MsgDelegate", "delegator_address": W}], 8396,
                   logs=[{"msg_index": 0, "events": [
                       _ev("coin_spent", spender=W, amount="107000000uatom"),
                       _ev("coin_received", receiver=BONDED, amount="107000000uatom")]}])
    send = _tx("S1", 200, "2025-01-07T20:20:33Z",
               [{"@type": "/cosmos.bank.v1beta1.MsgSend", "from_address": W, "to_address": OTHER}], 1008,
               events=[_ev("coin_spent", spender=W, amount="1008uatom"),  # ante handler: fee (no msg_index)
                       _ev("coin_received", receiver=FEEMKT, amount="1008uatom"),
                       _ev("coin_spent", 0, spender=W, amount="248000000uatom"),
                       _ev("coin_received", 0, receiver=OTHER, amount="248000000uatom")])
    moves, txs = normalize(CH, W, [send, delegate], MODULES, _meta)
    assert [(m.tx_hash, m.amount, m.counterparty) for m in moves] == [("D1", D("-107"), BONDED),
                                                                       ("S1", D("-248"), OTHER)]
    assert txs["D1"].gas_fee == D("0.008396") and txs["S1"].gas_fee == D("0.001008")
    assert txs["S1"].tx_from == W and txs["D1"].tx_to == "/cosmos.staking.v1beta1.MsgDelegate"


def test_rewards_any_denom_and_unbonding_completion() -> None:
    undelegate = _tx("U1", 300, "2024-10-14T11:29:15Z",
                     [{"@type": "/cosmos.staking.v1beta1.MsgUndelegate", "delegator_address": W}], 3363,
                     events=[_ev("coin_spent", 0, spender=DISTR, amount="34uatom,6573ibc/NTRN"),
                             _ev("coin_received", 0, receiver=W, amount="34uatom,6573ibc/NTRN"),
                             _ev("unbond", 0, amount="107000000uatom", completion_time="2024-11-04T11:29:15Z")])
    moves, txs = normalize(CH, W, [undelegate], MODULES, _meta)
    got = {(m.symbol, m.amount, m.source) for m in moves}
    assert ("ATOM", D("0.000034"), "reward_withdrawal") in got and ("NTRN", D("0.006573"), "reward_withdrawal") in got
    unb = next(m for m in moves if m.source == "unbonding_complete")
    assert (unb.amount, unb.tx_hash, unb.counterparty) == (D("107"), "unbond:U1", "cosmos:not_bonded_pool")
    assert txs["unbond:U1"].timestamp == 1730719755  # 2024-11-04T11:29:15Z


def test_ibc_receipt_relayed_by_third_party_names_the_sender_and_pays_no_gas() -> None:
    recv = _tx("R1", 400, "2025-01-07T20:15:05Z",
               [{"@type": "/ibc.core.client.v1.MsgUpdateClient", "signer": "cosmos1relayer"},
                {"@type": "/ibc.core.channel.v1.MsgRecvPacket", "signer": "cosmos1relayer"}], 2907,
               events=[_ev("coin_spent", spender="cosmos1relayer", amount="2907uatom"),
                       _ev("coin_spent", 1, spender=ESCROW, amount="131222603uatom"),
                       _ev("coin_received", 1, receiver=W, amount="131222603uatom"),
                       _ev("fungible_token_packet", 1, sender="osmo1router", receiver=W,
                           denom="transfer/channel-0/uatom", amount="131222603", success="true")],
               memo="Relayed by Example | hermes (https://hermes.informal.systems)")
    (m,), txs = normalize(CH, W, [recv], MODULES, _meta)
    assert (m.amount, m.counterparty, m.source) == (D("131.222603"), "ibc:osmo1router", "ibc_receive")
    assert txs["R1"].tx_from == "cosmos1relayer" and txs["R1"].meta["ibc_senders"] == ["osmo1router"]
    assert "note" not in txs["R1"].meta and txs["R1"].meta["relayer_memo"].startswith("Relayed by")


def test_multisend_spam_keeps_the_memo_for_the_phishing_rule() -> None:
    spam = _tx("M1", 500, "2025-01-28T11:19:52Z",
               [{"@type": "/cosmos.bank.v1beta1.MsgMultiSend", "inputs": [{"address": "cosmos1spammer"}]}], 195000,
               events=[_ev("coin_spent", 0, spender="cosmos1spammer", amount="3000uatom"),
                       _ev("coin_received", 0, receiver=W, amount="1uatom"),
                       _ev("coin_received", 0, receiver="cosmos1x", amount="1uatom")],
               memo="Account is listed in AIRDROP, check https://example.invalid")
    (m,), txs = normalize(CH, W, [spam], MODULES, _meta)
    assert m.amount == D("0.000001") and txs["M1"].tx_from == "cosmos1spammer"
    assert "https://" in txs["M1"].meta["note"]


def test_base_denom_decimals() -> None:
    assert base_meta("uatom") == ("ATOM", 6) and base_meta("aevmos") == ("EVMOS", 18)
    assert base_meta("stuatom") == ("stATOM", 6) and base_meta("stinj") == ("stINJ", 18)
    assert base_meta("staevmos") == ("stEVMOS", 18) and base_meta("factory/osmo1x/alloyed") == ("ALLOYED", 6)


DY = load_settings(None).chains["dYdX"]
DW = "dydx1wallet"


def test_full_hash_list_rows_need_no_height(tmp_path: Path) -> None:
    p = tmp_path / "txs.csv"
    full = "8BCA5F8E46813EB2F7093081080941E9CC69BD93849FA1E00A68B020E7CE136B"
    p.write_text(f"Address,Height,Hash\n{DW},,{full}\n{DW},,8BCA...136B\n")
    (r,) = read_tx_list(p)  # a shortened hash without its height can't be resolved: skipped
    assert r.is_full and r.height == 0 and r.matches(full)
    assert read_tx_list(tmp_path / "missing.csv", required=False) == []


def test_dydx_bridge_credit_and_unbonding_waits_for_completion() -> None:
    undelegate = _tx("U2", 114804261, "2026-11-21T20:17:28Z",
                     [{"@type": "/cosmos.staking.v1beta1.MsgUndelegate", "delegator_address": DW}], 0,
                     events=[_ev("unbond", 0, amount="140000000000000000000adydx",
                                 completion_time="2026-12-12T20:17:28Z")])
    undelegate["tx"]["auth_info"]["fee"]["amount"] = [{"denom": "adydx", "amount": "2580812500000000"}]
    credit = {"id": 5060, "denom": "adydx", "amount": "142410000000000000000", "eth_block": "19629670",
              "ack_tx": "8BCA", "height": 13059865, "time": "2024-04-12T06:42:06.400090382Z"}
    before = 1795300000  # 2026-11-21, during the 21-day unbonding
    moves, txs = normalize(DY, DW, [undelegate], {}, _meta, [credit], now=before)
    assert [(m.tx_hash, m.amount, m.source, m.symbol) for m in moves] == [("bridge:5060", D("142.41"), "bridge_mint", "DYDX")]
    assert txs["bridge:5060"].tx_to == "dydx:bridge" and txs["U2"].gas_fee == D("0.0025808125")
    moves, _ = normalize(DY, DW, [undelegate], {}, _meta, [credit], now=1797200000)  # 2026-12-13
    unb = [m for m in moves if m.source == "unbonding_complete"]
    assert len(unb) == 1 and unb[0].amount == D(140) and unb[0].counterparty == "dydx:not_bonded_pool"
