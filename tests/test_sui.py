"""Sui normalization (balance changes, net gas incl. rebates) and DeepTrade order lifecycle."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from loledger.adapters.sui.normalize import SUI_TYPE, normalize
from loledger.config import load_settings
from loledger.decode.sui_orders import classify_sui_wallet
from loledger.registry import load_registry
from loledger.rules import Cls, RuleContext

SUI = load_settings().chains["Sui"]
W = "0x" + "f4" * 32
KEEPER = "0x" + "99" * 32
ETH = "0xd0e89b2af5e4910726fbcd8b8dd37bb79b29e5f83f7491bca830e94f7f226d29::eth::ETH"
LO = "0x533fab9a116080e2cb1c87f1832c1bf4231ab4c32318ced041e75cc28604bba9"


def tx(d: str, ts: str, sender: str, changes: list[tuple[str, str, int]], gas: tuple[int, int, int] = (1000, 2000, 500),
       calls: list[str] | None = None, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"digest": d, "sender": {"address": sender},
            "kind": {"commands": {"nodes": [{"function": {"name": c.split("::")[2], "module": {"name": c.split("::")[1],
                                                                                                  "package": {"address": c.split("::")[0]}}}}
                                            for c in calls or []]}},
            "effects": {"status": "SUCCESS", "timestamp": ts, "checkpoint": {"sequenceNumber": 1},
                        "gasEffects": {"gasSummary": {"computationCost": gas[0], "storageCost": gas[1], "storageRebate": gas[2],
                                                      "nonRefundableStorageFee": 0}},
                        "balanceChanges": {"nodes": [{"owner": {"address": o}, "coinType": {"repr": c}, "amount": str(a)}
                                                     for o, c, a in changes]},
                        "events": {"nodes": [{"contents": {"type": {"repr": e["type"]}, "json": e["json"]}} for e in events or []]}}}


def meta(ct: str) -> tuple[str, int]:
    return ("ETH", 8) if ct == ETH else ("X", 6)


def test_gas_and_negative_rebate_reconcile() -> None:
    send = tx("d1", "2026-02-12T19:12:00Z", W, [(W, SUI_TYPE, -1_000_002_500)], gas=(1000, 2000, 500))  # 1 SUI + 2500 gas
    rebate = tx("d2", "2026-03-14T17:58:00Z", W, [(W, SUI_TYPE, 719_848)], gas=(0, 0, 719_848))  # net gas -719848
    moves, txs = normalize(SUI, W, [send, rebate], meta)
    total = sum((m.amount for m in moves), Decimal(0)) - sum((t.gas_fee for t in txs.values()), Decimal(0))
    assert total == Decimal(-1_000_002_500 + 719_848).scaleb(-9)
    assert txs["d2"].gas_fee == Decimal(-719_848).scaleb(-9)


def test_deeptrade_order_placed_then_filled_by_keeper() -> None:
    oid = "0xabc"
    place = tx("p1", "2025-01-27T02:43:00Z", W, [(W, ETH, -12_921_060), (W, SUI_TYPE, -2500)],
               calls=[f"{LO}::limit_order::place_limit_order"],
               events=[{"type": f"{LO}::limit_order::OrderPlacedEvent",
                        "json": {"order_id": oid, "owner": W, "pay_coin": ETH[2:], "target_coin": SUI_TYPE[2:],
                                 "total_pay_amount": "12921060", "rate": "1165", "expire_ts": "1738556636000"}}])
    fill = tx("f1", "2025-01-28T00:00:00Z", KEEPER, [(W, SUI_TYPE, 151_000_000_000)],
              calls=[f"{LO}::limit_order::cancel_order_by_keeper"],
              events=[{"type": f"{LO}::limit_order::OrderCanceledEvent",
                       "json": {"order_id": oid, "owner": W, "total_pay_amount": "12921060", "remaining_pay_amount": "0",
                                "remaining_target_amount": "0", "cancel_reason": "3"}}])
    moves, txs = normalize(SUI, W, [place, fill], meta)
    ctx = RuleContext(chain=SUI, wallets={W}, registry=load_registry("Sui"))
    rows = classify_sui_wallet(W, moves, txs, ctx)
    placed = [r for r in rows if r.tx.tx_hash == "p1" and r.leg_role == "order"]
    filled = [r for r in rows if r.tx.tx_hash == "f1"]
    assert placed and placed[0].subtype == "deeptrade_limit:filled" and placed[0].linked_tx_hash == "f1"
    assert [(r.classification, r.linked_tx_hash) for r in filled] == [(Cls.ORDER_FILLED, "p1")]
    assert placed[0].gas_fee_native is not None and filled[0].gas_fee_native is None


def test_registry_keeps_move_type_case() -> None:
    reg = load_registry("Sui")
    assert ETH in reg.tokens and reg.tokens[ETH] == "ETH"
