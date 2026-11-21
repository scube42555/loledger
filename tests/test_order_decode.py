"""Order decoding: Cardano datum schemas and Kyber/Gelato EVM events."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from eth_abi.abi import encode

from loledger.decode.cardano_datums import decode
from loledger.decode.evm_orders import GELATO_DEPOSIT, KYBER_CANCELED, KYBER_FILLED, annotate_gelato, annotate_kyber
from loledger.model import AssetKind, Log, Movement, TxContext
from loledger.registry import ContractInfo, Registry
from loledger.rules import Classified, Cls

SNEK_P, SNEK_N = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f", "534e454b"


def C(n: int, *fields: Any) -> dict[str, Any]:
    return {"constructor": n, "fields": list(fields)}


def B(h: str) -> dict[str, str]:
    return {"bytes": h}


def I(n: int) -> dict[str, int]:
    return {"int": n}


ADDR = C(0, C(0, B("aa" * 28)), C(0, C(0, C(0, B("bb" * 28)))))


def test_spectrum_datum() -> None:
    d = C(0, C(0, B(""), B("")), C(0, B(SNEK_P), B(SNEK_N)), C(0, B("cc" * 28), B("dd")), I(990), I(65), I(10),
          B("ee" * 28), C(0, B("ff" * 28)), I(500_000_000), I(230_096))
    t = decode("spectrum", d)
    assert t and (t.sell_asset, t.sell_amount, t.buy_asset, t.min_receive) == ("native", 500_000_000,
                                                                              f"{SNEK_P}.{SNEK_N}", 230_096)


def test_muesli_orderbook_datum() -> None:
    d = C(0, C(0, ADDR, B(SNEK_P), B(SNEK_N), B(""), B(""), I(7_760_600_980), C(1), I(2_650_000)))
    t = decode("muesliswap-orderbook", d)
    assert t and (t.buy_asset, t.min_receive, t.allow_partial, t.fee, t.sell_asset) == (
        f"{SNEK_P}.{SNEK_N}", 7_760_600_980, True, 2_650_000, "native")


def test_minswap_v1_and_v2_datums() -> None:
    v1 = C(0, ADDR, ADDR, C(1), C(0, C(0, B(""), B("")), I(1_055_496_000)), I(2_000_000), I(2_000_000))
    t1 = decode("minswap-v1", v1)
    assert t1 and (t1.buy_asset, t1.min_receive, t1.fee) == ("native", 1_055_496_000, 2_000_000)
    v2 = C(0, C(0, B("aa" * 28)), ADDR, C(0), ADDR, C(0), C(0, B("f5" * 28), B("2f")),
           C(0, C(0), C(0, I(250_000)), I(547_118_953), C(0)), I(1_300_000), C(0, I(1_760_000_000_000)))
    t2 = decode("minswap-v2", v2)
    assert t2 and (t2.sell_amount, t2.min_receive, t2.expiry_ms) == (250_000, 547_118_953, 1_760_000_000_000)


def test_vyfi_datum_and_unknown_shape() -> None:
    t = decode("vyfi", C(0, B("aa" * 28), C(4, I(394_500_000))))
    assert t and t.min_receive == 394_500_000 and t.step == "vyfi_op4"
    assert decode("spectrum", C(0, I(1))) is None


# ---- EVM -----------------------------------------------------------------------------------------

W = "0x" + "a1" * 20
DSLO = "0x" + "d5" * 20
LINK, USDT = "0x" + "c2" * 20, "0x" + "c3" * 20
GROUTER = "0x" + "90" * 20
REG = Registry(contracts={DSLO: ContractInfo("DSLOProtocol", "limit_order_protocol", "kyberswap-limit-order"),
                          GROUTER: ContractInfo("ERC20OrderRouter", "order_router", "gelato-limit-order")})


def _tx(h: str, ts: int, logs: list[Log], frm: str = "0x" + "99" * 20) -> TxContext:
    return TxContext(chain="Avalanche", tx_hash=h, block=ts, timestamp=ts, tx_from=frm, tx_to=DSLO, method_id="",
                     function_name="", is_error=False, gas_fee=Decimal("0.01"), value=Decimal(0), logs=logs)


def _row(tx: TxContext, idx: int, asset: str, amount: str, dec: int, sym: str, role: str,
         cls: Cls = Cls.ORDER_FILLED, sub: str = "signed_limit_order", source: str = "tokentx") -> Classified:
    m = Movement(chain="Avalanche", tx_hash=tx.tx_hash, leg_id=f"log:{idx}" if source != "gas" else "gas",
                 block=tx.block, timestamp=tx.timestamp, wallet=W, asset_id=asset, asset_kind=AssetKind.ERC20,
                 symbol=sym, decimals=dec, amount=Decimal(amount), counterparty=DSLO, source=source)
    return Classified(m, tx, cls, sub, "kyberswap-limit-order", 1.0, "", role)


def _filled(idx: int, oh: str, remaining: int, making: int, taking: int) -> Log:
    return Log(DSLO, (KYBER_FILLED, "0x" + "77" * 32, oh), "0x" + encode(["uint256"] * 3, [remaining, making, taking]).hex(), idx)


def test_kyber_batched_fills_events_precede_their_transfers_and_cancel_links() -> None:
    oh = "0x" + "e3" * 32
    # one tx fills the same order twice: event, then maker out+fee, then maker in
    t1 = _tx("0xf1", 10, [_filled(25, oh, 19 * 10**18, 12 * 10**18, 229_000_000),
                          _filled(43, oh, 10**18, 18999 * 10**15, 344_000_000)])
    rows = [_row(t1, 26, LINK, "-11.988", 18, "LINK.e", "out"), _row(t1, 29, LINK, "-0.012", 18, "LINK.e", "fee"),
            _row(t1, 31, USDT, "229", 6, "USDT.e", "in"),
            _row(t1, 44, LINK, "-18.98", 18, "LINK.e", "out"), _row(t1, 47, LINK, "-0.019", 18, "LINK.e", "fee"),
            _row(t1, 49, USDT, "344", 6, "USDT.e", "in")]
    pad = "0x" + W[2:].rjust(64, "0")
    t2 = _tx("0xc1", 20, [Log(DSLO, (KYBER_CANCELED, pad), "0x" + encode(["bytes32", "uint256"], [bytes.fromhex("e3" * 32), 10**18]).hex(), 3)], frm=W)
    rows.append(_row(t2, 0, "native", "0", 18, "AVAX", "", cls=Cls.OTHER, sub="contract_call:cancelBatchOrders", source="gas"))
    orders = annotate_kyber(rows, REG)
    assert set(orders) == {oh}
    assert all(r.order_id == f"kyber:{oh}" for r in rows)
    assert "WARNING" not in "".join(r.reason for r in rows)
    assert {r.subtype for r in rows[:6]} == {"signed_limit_order:partial→cancelled"}
    assert rows[6].classification == Cls.ORDER_CANCELLED and rows[6].linked_tx_hash == "0xf1"
    assert "fill 2/2" in rows[3].order_details and "order size 31" in rows[0].order_details


def test_gelato_deposit_token_terms() -> None:
    key = "0x" + "4b" * 32
    data = encode(["address", "uint256"], [LINK, 11_226_684_000_000_000_000])
    payload = encode(["uint256", "address", "address", "address", "bytes", "bytes32"],
                     [199_000_000, "0x" + "01" * 20, USDT, "0x" + "02" * 20, data, b"\x00" * 32])
    t = _tx("0xg1", 1, [Log(GROUTER, (GELATO_DEPOSIT, key, "0x" + "00" * 32, "0x" + "00" * 32), "0x" + payload.hex(), 0)], frm=W)
    r = _row(t, 1, USDT, "-199", 6, "USDT.e", "out", cls=Cls.ORDER_PLACED, sub="escrow_cancelled")
    annotate_gelato([r], REG, {USDT: ("USDT.e", 6), LINK: ("LINK.e", 18)})
    assert r.order_id == f"gelato:{key}"
    assert r.order_details.startswith("sell 199.000000 USDT.e for ≥11.226684 LINK.e; limit 17.72")
