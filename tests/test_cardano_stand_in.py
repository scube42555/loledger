"""A Cardano tx a wallet signed but that was only stored through another wallet's sync."""

from __future__ import annotations

from decimal import Decimal

from loledger.config import load_settings
from loledger.decode.cardano_orders import CardanoState, classify_cardano_wallet
from loledger.model import TxContext
from loledger.registry import Registry
from loledger.rules import RuleContext


def _tx(h: str, block: int) -> TxContext:
    return TxContext(chain="Cardano", tx_hash=h, block=block, timestamp=block, tx_from="stake1mid", tx_to="addr1qother",
                     method_id="", function_name="", is_error=False, gas_fee=Decimal("0.17"), value=Decimal(0),
                     meta={"inputs": [], "script_outputs": {}, "certs": []})


def test_signed_tx_past_the_wallets_own_history_is_marked_awaiting_sync() -> None:
    chain = load_settings(None).chains["Cardano"]
    txs = {"old": _tx("old", 100), "new": _tx("new", 200)}
    ctx = RuleContext(chain=chain, wallets={"stake1mid"}, registry=Registry(discovered={}))
    rows = classify_cardano_wallet("stake1mid", [], txs, ctx, CardanoState(scripts={}), synced_through=150)
    by = {r.tx.tx_hash: r for r in rows}
    assert by["old"].subtype == "self_send"  # in its own history with no movement: a genuine fee-only tx
    assert by["new"].subtype == "awaiting_sync" and "Re-sync this wallet" in by["new"].reason
    assert by["new"].gas_fee_native == Decimal("0.17")  # the fee it paid is still booked
