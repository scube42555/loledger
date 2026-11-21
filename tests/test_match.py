"""Heuristic matcher: positive cases, and cases that must NOT match."""

from __future__ import annotations

from decimal import Decimal

from loledger.match import match_flows
from loledger.model import AssetKind, Movement, TxContext
from loledger.rules import Classified, Cls

T0 = 1_770_000_000


def ev(chain: str, h: str, ts: int, sym: str, amount: str, usd: str, cls: Cls, sub: str, *, cp: str = "x",
       order_id: str = "", details: str = "", wallet: str = "w") -> Classified:
    tx = TxContext(chain=chain, tx_hash=h, block=ts, timestamp=ts, tx_from=wallet, tx_to=cp, method_id="",
                   function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0))
    m = Movement(chain=chain, tx_hash=h, leg_id="l", block=ts, timestamp=ts, wallet=wallet, asset_id=sym,
                 asset_kind=AssetKind.ERC20, symbol=sym, decimals=6, amount=Decimal(amount), counterparty=cp,
                 source="tokentx")
    r = Classified(m, tx, cls, sub, "", 1.0, "")
    r.usd_value, r.order_id, r.order_details = Decimal(usd), order_id, details
    return r


def test_exchanger_swap_matched_and_relabelled() -> None:
    out = ev("Avalanche", "a", T0, "AVAX", "-192", "-1623.47", Cls.TRANSFER, "to_eoa", cp="dep")
    inn = ev("Avalanche", "b", T0 + 100, "USDC", "1591", "1590.76", Cls.TRANSFER, "from_eoa", cp="pay")
    [m] = match_flows([out, inn])
    assert m.kind == "exchanger_swap" and out.linked_tx_hash == "b" and inn.linked_tx_hash == "a"
    assert (out.subtype, inn.subtype) == ("exchanger_swap_out", "exchanger_swap_in")
    assert 0.5 < out.confidence < 1.0


def test_value_mismatch_or_long_gap_is_not_matched() -> None:
    out = ev("Avalanche", "a", T0, "AVAX", "-192", "-1623", Cls.TRANSFER, "to_eoa")
    cheap = ev("Avalanche", "b", T0 + 100, "USDC", "900", "900", Cls.TRANSFER, "from_eoa")
    late = ev("Avalanche", "c", T0 + 10 * 86_400, "USDC", "1590", "1590", Cls.TRANSFER, "from_eoa")
    assert match_flows([out, cheap, late]) == []
    assert out.linked_tx_hash == "" and out.confidence == 1.0


def test_same_asset_same_chain_is_not_a_bridge() -> None:
    out = ev("Avalanche", "a", T0, "USDC", "-100", "-100", Cls.TRANSFER, "to_eoa")
    inn = ev("Avalanche", "b", T0 + 60, "USDC", "99", "99", Cls.TRANSFER, "from_eoa")
    assert match_flows([out, inn]) == []


def test_bridge_across_chains_same_asset_family() -> None:
    out = ev("Avalanche", "a", T0, "USDC.e", "-1717", "-1717", Cls.OTHER, "bridge_out")
    inn = ev("Ethereum", "b", T0 + 1800, "USDC", "1712", "1712", Cls.TRANSFER, "from_contract")
    [m] = match_flows([out, inn])
    assert m.kind == "bridge" and inn.subtype == "bridge_in"


def test_unresolved_order_matched_by_shared_script_and_limit_check() -> None:
    order = ev("Cardano", "o", T0, "SNEK", "-180000", "-674", Cls.ORDER_PLACED, "market:spent_outside_history",
               cp="script1", order_id="cardano:o#0")
    paid = ev("Cardano", "p", T0 + 30, "ADA", "697", "685", Cls.TRANSFER, "from_contract", cp="script1")
    [m] = match_flows([order, paid])
    assert m.kind == "order_fill" and paid.classification == Cls.ORDER_FILLED
    assert order.subtype == "market:heuristic_filled"
    # decoded min-receive not met -> no match
    order2 = ev("Cardano", "o2", T0, "SNEK", "-180000", "-674", Cls.ORDER_PLACED, "limit:open", cp="script1",
                order_id="cardano:o2#0", details="sell 180000 SNEK; for ≥800 ADA; limit …")
    paid2 = ev("Cardano", "p2", T0 + 30, "ADA", "697", "685", Cls.TRANSFER, "from_contract", cp="script1")
    assert match_flows([order2, paid2]) == []


def test_greedy_assignment_is_one_to_one() -> None:
    out = ev("Avalanche", "a", T0, "AVAX", "-10", "-100", Cls.TRANSFER, "to_eoa")
    near = ev("Avalanche", "b", T0 + 60, "USDC", "98", "98", Cls.TRANSFER, "from_eoa")
    later = ev("Avalanche", "c", T0 + 3600, "USDC", "98", "98", Cls.TRANSFER, "from_eoa")
    [m] = match_flows([out, near, later])
    assert m.inn.tx == "b" and later.linked_tx_hash == ""
