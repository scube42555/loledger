"""EVM limit-order event decoding: link fills/cancels by order hash and extract limit prices.

KyberSwap Limit Order (DSLOProtocol), signed off-chain:
  OrderFilled(taker idx, orderHash idx, remaining, makingAmount, takingAmount)
    makingAmount = principal + 0.1% fee (both maker legs), takingAmount = maker's inflow.
    OrderFilled is emitted before its transfers, so each maker leg belongs to the last
    OrderFilled log preceding it (verified on batched fills of several orders in one tx).
  OrderCanceled(maker idx, orderHash, remainingRaw)
Gelato limit orders (escrow):
  ERC20OrderRouter.DepositToken(key idx, caller idx, amount, module, inputToken, owner idx, witness, data, secret)
    data = abi.encode(outputToken, minReturn)
  GelatoPineCore.OrderCancelled(key idx, ...), OrderExecuted(key idx, ..., amount, bought)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_utils.crypto import keccak

from ..model import Log, Movement
from ..registry import Registry
from ..rules import Classified, Cls


def _topic(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


KYBER_FILLED = _topic("OrderFilled(address,bytes32,uint256,uint256,uint256)")
KYBER_CANCELED = _topic("OrderCanceled(address,bytes32,uint256)")
GELATO_DEPOSIT = _topic("DepositToken(bytes32,address,uint256,address,address,address,address,bytes,bytes32)")
GELATO_CANCELLED = _topic("OrderCancelled(bytes32,address,address,address,bytes,uint256)")
GELATO_EXECUTED = _topic("OrderExecuted(bytes32,address,address,address,bytes,bytes,uint256,uint256)")


def _data(lg: Log, types: list[str]) -> tuple[Any, ...]:
    return tuple(abi_decode(types, bytes.fromhex(lg.data[2:])))


def _raw(m: Movement) -> int:
    return int(abs(m.amount).scaleb(m.decimals))


def _leg_log_index(m: Movement) -> int | None:
    if m.leg_id.startswith("log:"):
        try:
            return int(m.leg_id.split(":")[1].split("#")[0])
        except ValueError:
            return None
    return None


def _price(sell: Decimal, sell_sym: str, buy: Decimal, buy_sym: str) -> str:
    """Quote stablecoin/native-per-token style: price of the non-stable side in the other unit."""
    if not sell or not buy:
        return ""
    stable = {"USDC", "USDT", "USDT.E", "USDC.E", "DAI.E", "USDT"}
    if sell_sym.upper() in stable or (sell_sym.upper() in ("AVAX", "WAVAX") and buy_sym.upper() not in stable):
        return f"{(sell / buy):.8g} {sell_sym}/{buy_sym}"
    return f"{(buy / sell):.8g} {buy_sym}/{sell_sym}"


@dataclass
class KyberFill:
    tx: str
    ts: int
    remaining: int
    making: int
    taking: int
    rows: list[Classified]
    sell: Movement
    buy: Movement | None


@dataclass
class KyberOrder:
    order_hash: str
    fills: list[KyberFill] = field(default_factory=list)
    cancel_rows: list[Classified] = field(default_factory=list)
    cancel_remaining: int | None = None


def annotate_kyber(rows: list[Classified], reg: Registry) -> dict[str, KyberOrder]:
    dslo = {a for a, c in reg.contracts.items() if c.kind == "limit_order_protocol" and "kyber" in c.protocol}
    orders: dict[str, KyberOrder] = {}
    groups: dict[tuple[str, str], list[Classified]] = defaultdict(list)
    for r in rows:
        if r.classification == Cls.ORDER_FILLED and r.subtype.startswith("signed_limit_order"):
            groups[(r.movement.wallet, r.tx.tx_hash)].append(r)
    for (_, h), rs in groups.items():
        tx = rs[0].tx
        events = sorted((lg.log_index, lg) for lg in tx.logs
                        if lg.address in dslo and lg.topics and lg.topics[0] == KYBER_FILLED)
        assigned: dict[int, list[Classified]] = defaultdict(list)
        for r in rs:
            li = _leg_log_index(r.movement)
            ev = max((i for i, _ in events if li is not None and i < li), default=None)
            if ev is not None:
                assigned[ev].append(r)
        for i, lg in events:
            mine = assigned.get(i)
            if not mine:
                continue  # another maker's order in the same batch
            remaining, making, taking = (int(x) for x in _data(lg, ["uint256", "uint256", "uint256"]))
            oh = lg.topics[2]
            outs = [r for r in mine if r.movement.amount < 0]
            ins = [r for r in mine if r.movement.amount > 0]
            ok = sum(_raw(r.movement) for r in outs) == making and sum(_raw(r.movement) for r in ins) == taking
            sell = max(outs, key=lambda r: abs(r.movement.amount)).movement if outs else mine[0].movement
            fill = KyberFill(h, tx.timestamp, remaining, making, taking, mine, sell, ins[0].movement if ins else None)
            orders.setdefault(oh, KyberOrder(oh)).fills.append(fill)
            for r in mine:
                r.order_id = f"kyber:{oh}"
                if not ok:
                    r.reason += "; WARNING: legs do not add up to the OrderFilled amounts"
    # cancels: gas-only rows of wallet-sent txs that emitted OrderCanceled for the wallet as maker
    for r in rows:
        if r.movement.source != "gas" or r.tx.tx_from != r.movement.wallet:
            continue
        pad = "0x" + r.movement.wallet[2:].rjust(64, "0")
        for lg in r.tx.logs:
            if lg.address in dslo and lg.topics and lg.topics[0] == KYBER_CANCELED and lg.topics[1] == pad:
                oh_bytes, rem = _data(lg, ["bytes32", "uint256"])
                oh = "0x" + bytes(oh_bytes).hex()
                o = orders.setdefault(oh, KyberOrder(oh))
                o.cancel_rows.append(r)
                o.cancel_remaining = int(rem)
                r.classification, r.subtype = Cls.ORDER_CANCELLED, "signed_limit_order"
                r.protocol = reg.contracts[lg.address].protocol
                r.order_id = f"kyber:{oh}"
                r.reason = "wallet cancelled signed limit order(s) on-chain (OrderCanceled event)"
    for o in orders.values():
        _describe_kyber(o)
    return orders


def _describe_kyber(o: KyberOrder) -> None:
    fills = sorted(o.fills, key=lambda f: (f.ts, f.tx))
    if fills:
        f0 = fills[0]
        dec_s = f0.sell.decimals
        size = f0.making + f0.remaining
        made = sum(f.making for f in fills)
        last_rem = fills[-1].remaining
        done = last_rem * 10_000 <= size  # dust (<0.01%) left counts as filled
        status = "filled" if done else ("partial→cancelled" if o.cancel_rows else "partial_rest_unfilled")
        sell_sym = f0.sell.symbol
        buy_sym = f0.buy.symbol if f0.buy else "?"
        dec_b = f0.buy.decimals if f0.buy else 0
        lim = _price(Decimal(f0.making).scaleb(-dec_s), sell_sym, Decimal(f0.taking).scaleb(-dec_b), buy_sym)
        terms = (f"order size {Decimal(size).scaleb(-dec_s):.8g} {sell_sym} (incl. 0.1% fee); "
                 f"filled {Decimal(made).scaleb(-dec_s):.8g} ({made * 100 / size:.1f}%) in {len(fills)} fill(s); "
                 f"limit {lim}; status {status}")
        for k, f in enumerate(fills):
            exe = _price(Decimal(f.making).scaleb(-dec_s), sell_sym, Decimal(f.taking).scaleb(-dec_b), buy_sym)
            for r in f.rows:
                r.subtype = f"signed_limit_order:{status}"
                r.order_details = f"fill {k + 1}/{len(fills)} at {exe}; {terms}"
                r.linked_tx_hash = fills[0].tx if k else (fills[1].tx if len(fills) > 1 else "")
        for r in o.cancel_rows:
            r.linked_tx_hash = fills[-1].tx
            r.order_details = f"cancelled remaining {Decimal(o.cancel_remaining or 0).scaleb(-dec_s):.8g} {sell_sym}; {terms}"
    else:
        for r in o.cancel_rows:
            r.order_details = "cancelled before any fill (no OrderFilled seen for this order hash)"


def annotate_gelato(rows: list[Classified], reg: Registry, assets: dict[str, tuple[str, int]]) -> None:
    routers = {a for a, c in reg.contracts.items() if c.protocol == "gelato-limit-order"}
    by_key: dict[str, list[Classified]] = defaultdict(list)
    terms: dict[str, str] = {}
    for r in rows:
        for lg in r.tx.logs:
            if lg.address not in routers or not lg.topics:
                continue
            key = lg.topics[1] if len(lg.topics) > 1 else ""
            if lg.topics[0] == GELATO_DEPOSIT and r.classification == Cls.ORDER_PLACED:
                amount, _module, input_token, _witness, data, _secret = _data(
                    lg, ["uint256", "address", "address", "address", "bytes", "bytes32"])
                out_token, min_return = abi_decode(["address", "uint256"], bytes(data))
                in_sym, in_dec = assets.get(str(input_token).lower(), ("?", 18))
                out_sym, out_dec = assets.get(str(out_token).lower(), (str(out_token)[:10], 18))
                sell = Decimal(int(amount)).scaleb(-in_dec)
                minr = Decimal(int(min_return)).scaleb(-out_dec)
                terms[key] = (f"sell {sell:f} {in_sym} for ≥{minr:.8g} {out_sym}; "
                              f"limit {_price(sell, in_sym, minr, out_sym)}")
                by_key[key].append(r)
            elif lg.topics[0] in (GELATO_CANCELLED, GELATO_EXECUTED):
                by_key[key].append(r)
    for key, rs in by_key.items():
        for r in rs:
            r.order_id = f"gelato:{key}"
            r.order_details = terms.get(key, "") + (f"; status {r.subtype}" if r.classification == Cls.ORDER_PLACED else "")
