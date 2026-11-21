from __future__ import annotations

import dataclasses

from decimal import Decimal
from typing import Any

from loledger.model import AssetKind, Movement, TxContext
from loledger.overrides import apply_overrides
from loledger.rules import Classified, Cls


def _row(h: str, cp: str) -> Classified:
    tx = TxContext(chain="Bitcoin", tx_hash=h, block=1, timestamp=1, tx_from=cp, tx_to="w", method_id="",
                   function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0))
    m = Movement(chain="Bitcoin", tx_hash=h, leg_id="vout:0", block=1, timestamp=1, wallet="w", asset_id="native",
                 asset_kind=AssetKind.NATIVE, symbol="BTC", decimals=8, amount=Decimal("0.5"), counterparty=cp,
                 source="utxo")
    return Classified(m, tx, Cls.TRANSFER, "from_eoa", "", 0.9, "x")


def test_override_by_counterparty_and_by_hash() -> None:
    a, b, c = _row("h1", "giver"), _row("h2", "x"), _row("h3", "y")
    notes = apply_overrides([a, b, c], [
        {"chain": "Bitcoin", "counterparty": "giver", "classification": "OTHER", "subtype": "gift_received", "reason": "gift"},
        {"chain": "Bitcoin", "tx_hashes": ["H2"], "classification": "TRANSFER", "subtype": "from_exchange", "reason": "payout"},
        {"chain": "Bitcoin", "classification": "OTHER", "subtype": "too_broad"},  # names nothing: ignored
    ])
    assert (a.classification, a.subtype, a.confidence) == (Cls.OTHER, "gift_received", 1.0)
    assert a.reason.startswith("user-confirmed:")
    assert b.subtype == "from_exchange" and c.subtype == "from_eoa"
    assert notes[2].endswith("0 row(s)")


def test_gift_recipients_are_not_own_wallets_and_sends_to_them_are_gifts(tmp_path: Any) -> None:
    from loledger.config import load_settings, load_wallets, split_gifts
    from loledger.overrides import gift_rules

    p = tmp_path / "a.csv"
    p.write_text("Address,Chain,Label\nmine,Bitcoin,Cold\nkid,Bitcoin,Kid Gift\nshop,Bitcoin,Giftshop\n")
    own, gifts = split_gifts(load_wallets(p, load_settings()))
    assert [w.address for w in own] == ["mine", "shop"] and [g.address for g in gifts] == ["kid"]
    out, back = _row("h1", "kid"), _row("h2", "kid")
    out.movement = dataclasses.replace(out.movement, amount=Decimal("-0.1"))
    apply_overrides([out, back], gift_rules(gifts))
    assert (out.classification, out.subtype) == (Cls.OTHER, "gift_sent") and "Kid Gift" in out.reason
    assert back.subtype == "from_eoa"  # only outflows to the recipient are gifts


def test_injective_0x_address_converts_to_its_inj1_account() -> None:
    from loledger.config import evm_to_bech32

    assert evm_to_bech32("0x" + "00" * 20, "cosmos") == "cosmos1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqnrql8a"  # well-known
    assert evm_to_bech32("0x" + "AB" * 20, "inj").startswith("inj1")


def test_override_filters_by_token_and_sets_link_role_and_handles_unquoted_hex() -> None:
    h = "0x" + "0" * 62 + "ab"
    a, b = _row(h, "c"), _row(h, "c")
    a = dataclasses.replace(a, movement=dataclasses.replace(a.movement, symbol="LP"))
    apply_overrides([a, b], [{"chain": a.movement.chain, "tx_hash": 0xAB, "token": "LP", "classification": "ORDER_FILLED",
                              "subtype": "lp_add", "leg_role": "in", "linked_tx_hash": "PlaceSig"}])
    assert (a.classification, a.leg_role, a.linked_tx_hash) == (Cls.ORDER_FILLED, "in", "PlaceSig")
    assert b.classification == Cls.TRANSFER
