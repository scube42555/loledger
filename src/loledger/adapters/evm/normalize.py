"""Turn an EvmRawBundle into wallet-perspective Movements and per-tx TxContexts. Pure; no I/O."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Log, Movement, TxContext, scale
from .fetch import ACCOUNT_ACTIONS, EvmRawBundle, Row

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# WETH9-style wrapped native: deposit/withdraw emit these instead of Transfer, so the explorer's
# tokentx never shows wrap/unwrap legs.
DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
ATOMIC_COUNTERPARTY = "avalanche:atomic"
BEACON_CP = "eth2:beacon"
PROPOSER_CP = "eth2:block_proposer"


def _addr_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _hx(v: Any) -> int:
    if v is None or v == "":
        return 0
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)


def parse_logs(receipt: dict[str, Any] | None) -> list[Log]:
    if not receipt:
        return []
    return [
        Log(address=str(lg["address"]).lower(), topics=tuple(t.lower() for t in lg.get("topics", [])),
            data=str(lg.get("data", "0x")), log_index=_hx(lg.get("logIndex")))
        for lg in receipt.get("logs", [])
    ]


class _LegIds:
    """Guarantees unique leg ids per tx within one wallet's movements."""

    def __init__(self) -> None:
        self.used: dict[str, set[str]] = defaultdict(set)

    def take(self, tx: str, leg: str) -> str:
        used = self.used[tx]
        out, n = leg, 1
        while out in used:
            n += 1
            out = f"{leg}#{n}"
        used.add(out)
        return out


def _transfer_log_index(logs: list[Log], used: set[int], contract: str, frm: str, to: str,
                        *, value: int | None = None, token_id: int | None = None) -> int | None:
    for lg in logs:
        if lg.log_index in used or lg.address != contract or not lg.topics or lg.topics[0] != TRANSFER_TOPIC:
            continue
        if len(lg.topics) == 3 and value is not None:
            if (_addr_topic(lg.topics[1]), _addr_topic(lg.topics[2])) == (frm, to) and _hx(lg.data or "0x0") == value:
                used.add(lg.log_index)
                return lg.log_index
        elif len(lg.topics) == 4 and token_id is not None:
            if (_addr_topic(lg.topics[1]), _addr_topic(lg.topics[2])) == (frm, to) and _hx(lg.topics[3]) == token_id:
                used.add(lg.log_index)
                return lg.log_index
    return None


def build_txs(chain: ChainConfig, bundle: EvmRawBundle) -> dict[str, TxContext]:
    dec = chain.native_decimals
    when: dict[str, tuple[int, int]] = {}
    for action in ACCOUNT_ACTIONS:  # beacon withdrawals carry no tx hash
        for r in bundle.rows.get(action, []):
            when.setdefault(str(r["hash"]).lower(), (int(r["blockNumber"]), int(r["timeStamp"])))

    out: dict[str, TxContext] = {}
    for r in bundle.rows.get("txlist", []):
        h = str(r["hash"]).lower()
        rc = bundle.receipts.get(h)
        if rc and rc.get("effectiveGasPrice"):
            fee = _hx(rc["gasUsed"]) * _hx(rc["effectiveGasPrice"]) + _hx(rc.get("l1Fee"))
        else:
            fee = int(r["gasUsed"]) * int(r["gasPrice"])
        out[h] = TxContext(
            chain=chain.name, tx_hash=h, block=int(r["blockNumber"]), timestamp=int(r["timeStamp"]),
            tx_from=str(r["from"]).lower(), tx_to=str(r.get("to") or r.get("contractAddress") or "").lower(),
            method_id=str(r.get("methodId") or str(r.get("input", ""))[:10]), function_name=str(r.get("functionName") or ""),
            is_error=str(r.get("isError")) == "1" or str(r.get("txreceipt_status")) == "0",
            gas_fee=scale(fee, dec), value=scale(r.get("value") or 0, dec), input=str(r.get("input") or ""),
            logs=parse_logs(rc),
        )
    for h, (block, ts) in when.items():
        if h in out:
            continue
        rc = bundle.receipts.get(h) or {}
        tx = bundle.txs.get(h) or {}
        gas_price = rc.get("effectiveGasPrice") or tx.get("gasPrice")
        fee = _hx(rc.get("gasUsed")) * _hx(gas_price) + _hx(rc.get("l1Fee"))
        inp = str(tx.get("input") or "")
        out[h] = TxContext(
            chain=chain.name, tx_hash=h, block=block, timestamp=ts,
            tx_from=str(rc.get("from") or tx.get("from") or "").lower(),
            tx_to=str(rc.get("to") or tx.get("to") or rc.get("contractAddress") or "").lower(),
            method_id=inp[:10], function_name="",
            is_error=rc.get("status") == "0x0", gas_fee=scale(fee, dec),
            value=scale(_hx(tx.get("value")), dec), input=inp, logs=parse_logs(rc),
        )
    return out


def normalize(chain: ChainConfig, bundle: EvmRawBundle) -> tuple[list[Movement], dict[str, TxContext]]:
    wallet = bundle.wallet.lower()
    txs = build_txs(chain, bundle)
    legs = _LegIds()
    used_logs: dict[str, set[int]] = defaultdict(set)
    moves: list[Movement] = []

    def add(r: Row, *, leg: str, asset_id: str, kind: AssetKind, symbol: str, decimals: int,
            raw: int, frm: str, to: str, source: str) -> None:
        h = str(r["hash"]).lower()
        amount = scale(raw, decimals)
        self_transfer = frm == to == wallet

        def mk(leg_id: str, signed: Decimal, counterparty: str) -> Movement:
            return Movement(chain=chain.name, tx_hash=h, leg_id=leg_id, block=int(r["blockNumber"]),
                            timestamp=int(r["timeStamp"]), wallet=wallet, asset_id=asset_id,
                            asset_kind=kind, symbol=symbol, decimals=decimals, amount=signed,
                            counterparty=counterparty, source=source)

        if frm == wallet:
            moves.append(mk(legs.take(h, leg + (":out" if self_transfer else "")), -amount, to))
        if to == wallet:
            moves.append(mk(legs.take(h, leg + (":in" if self_transfer else "")), amount, frm))

    def add_native(r: Row, *, leg: str, frm: str, to: str, source: str) -> None:
        add(r, leg=leg, asset_id=NATIVE_ASSET, kind=AssetKind.NATIVE, symbol=chain.native_symbol,
            decimals=chain.native_decimals, raw=int(r["value"]), frm=frm, to=to, source=source)

    for r in bundle.rows.get("txlist", []):
        if str(r.get("isError")) == "1" or int(r.get("value") or 0) == 0:
            continue
        to = str(r.get("to") or r.get("contractAddress") or "").lower()
        add_native(r, leg="value", frm=str(r["from"]).lower(), to=to, source="txlist")

    ordinal: dict[tuple[str, str], int] = defaultdict(int)

    def nth(source: str, h: str) -> int:
        ordinal[(source, h)] += 1
        return ordinal[(source, h)]

    for r in bundle.rows.get("txlistinternal", []):
        h = str(r["hash"]).lower()
        n = nth("internal", h)
        if str(r.get("isError")) == "1" or int(r.get("value") or 0) == 0:
            continue
        to = str(r.get("to") or r.get("contractAddress") or "").lower()
        trace = str(r.get("traceId") or n)
        add_native(r, leg=f"internal:{trace}", frm=str(r["from"]).lower(), to=to, source="internal")

    for r in bundle.rows.get("tokentx", []):
        h = str(r["hash"]).lower()
        n = nth("tokentx", h)
        contract, frm, to = (str(r[k]).lower() for k in ("contractAddress", "from", "to"))
        value = int(r["value"])
        idx = _transfer_log_index(txs[h].logs, used_logs[h], contract, frm, to, value=value)
        add(r, leg=f"log:{idx}" if idx is not None else f"tokentx:{n}", asset_id=contract, kind=AssetKind.ERC20,
            symbol=str(r.get("tokenSymbol") or "?"), decimals=int(r.get("tokenDecimal") or 0), raw=value,
            frm=frm, to=to, source="tokentx")

    for r in bundle.rows.get("tokennfttx", []):
        h = str(r["hash"]).lower()
        n = nth("tokennfttx", h)
        contract, frm, to = (str(r[k]).lower() for k in ("contractAddress", "from", "to"))
        token_id = int(r["tokenID"])
        idx = _transfer_log_index(txs[h].logs, used_logs[h], contract, frm, to, token_id=token_id)
        add(r, leg=f"log:{idx}" if idx is not None else f"nft:{n}", asset_id=f"{contract}:{token_id}",
            kind=AssetKind.ERC721, symbol=str(r.get("tokenSymbol") or "?"), decimals=0, raw=1,
            frm=frm, to=to, source="tokennfttx")

    for r in bundle.rows.get("token1155tx", []):
        h = str(r["hash"]).lower()
        n = nth("token1155tx", h)
        contract, frm, to = (str(r[k]).lower() for k in ("contractAddress", "from", "to"))
        add(r, leg=f"erc1155:{n}", asset_id=f"{contract}:{int(r['tokenID'])}", kind=AssetKind.ERC1155,
            symbol=str(r.get("tokenSymbol") or "?"), decimals=0, raw=int(r.get("tokenValue") or 0),
            frm=frm, to=to, source="token1155tx")

    wrapped = chain.wrapped_native
    if wrapped:
        pad = "0x" + wallet[2:].rjust(64, "0")
        wsym = next((str(r.get("tokenSymbol")) for r in bundle.rows.get("tokentx", [])
                     if str(r["contractAddress"]).lower() == wrapped), "W" + chain.native_symbol)
        for h, t in txs.items():
            for lg in t.logs:
                if lg.address != wrapped or len(lg.topics) < 2 or lg.topics[1] != pad:
                    continue
                if lg.topics[0] == DEPOSIT_TOPIC:
                    sign = 1
                elif lg.topics[0] == WITHDRAWAL_TOPIC:
                    sign = -1
                else:
                    continue
                moves.append(Movement(
                    chain=chain.name, tx_hash=h, leg_id=legs.take(h, f"log:{lg.log_index}"), block=t.block,
                    timestamp=t.timestamp, wallet=wallet, asset_id=wrapped, asset_kind=AssetKind.ERC20,
                    symbol=wsym, decimals=chain.native_decimals,
                    amount=sign * scale(_hx(lg.data or "0x0"), chain.native_decimals),
                    counterparty=wrapped, source="wrap"))

    for r in bundle.rows.get("beacon", []):  # consensus-layer withdrawals (amount in gwei)
        block, ts = int(r["blockNumber"]), int(r["timestamp"])
        h = f"beacon:{block}"
        moves.append(Movement(
            chain=chain.name, tx_hash=h, leg_id=f"withdrawal:{r['withdrawalIndex']}", block=block, timestamp=ts,
            wallet=wallet, asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol,
            decimals=chain.native_decimals, amount=scale(int(r["amount"]), 9), counterparty=BEACON_CP,
            source="beacon"))
        t = txs.get(h) or TxContext(chain=chain.name, tx_hash=h, block=block, timestamp=ts, tx_from="",
                                    tx_to=BEACON_CP, method_id="", function_name="beacon_withdrawal",
                                    is_error=False, gas_fee=Decimal(0), value=Decimal(0), meta={"validators": []})
        t.meta.setdefault("validators", []).append(r.get("validatorIndex"))
        txs[h] = t

    for r in bundle.rows.get("proposer", []):  # execution-layer fees paid to the block's fee recipient
        block, ts = int(r["blockNumber"]), int(r["timeStamp"])
        h = f"block:{block}"
        moves.append(Movement(
            chain=chain.name, tx_hash=h, leg_id="proposer_reward", block=block, timestamp=ts, wallet=wallet,
            asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol,
            decimals=chain.native_decimals, amount=scale(int(r["blockReward"]), chain.native_decimals),
            counterparty=PROPOSER_CP, source="proposer"))
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=block, timestamp=ts, tx_from="", tx_to=PROPOSER_CP,
                           method_id="", function_name="proposer_reward", is_error=False, gas_fee=Decimal(0),
                           value=Decimal(0))

    for c in bundle.atomic:
        h = f"atomic:{c.block}"
        ts = bundle.block_ts.get(c.block, 0)
        kind = "atomic_import" if c.amount > 0 else "atomic_export"
        moves.append(Movement(
            chain=chain.name, tx_hash=h, leg_id="atomic", block=c.block, timestamp=ts, wallet=wallet,
            asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol,
            decimals=chain.native_decimals, amount=c.amount, counterparty=ATOMIC_COUNTERPARTY, source="atomic"))
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=c.block, timestamp=ts, tx_from="",
                           tx_to=ATOMIC_COUNTERPARTY, method_id="", function_name=kind, is_error=False,
                           gas_fee=Decimal(0), value=abs(c.amount))

    moves.sort(key=lambda m: (m.timestamp, m.block, m.tx_hash, m.leg_id))
    return moves, txs


def native_deltas(moves: list[Movement], txs: dict[str, TxContext], wallet: str) -> dict[int, Decimal]:
    """Per-block native balance change implied by EVM data (movements + gas), excluding atomic legs."""
    out: dict[int, Decimal] = defaultdict(Decimal)
    for m in moves:
        if m.asset_id == NATIVE_ASSET and m.source not in ("atomic", "beacon", "proposer"):
            out[m.block] += m.amount
    for t in txs.values():
        if t.tx_from == wallet:
            out[t.block] -= t.gas_fee
    return dict(out)


def balances(moves: list[Movement], txs: dict[str, TxContext], wallet: str) -> dict[str, Decimal]:
    """Sum of signed movements per asset, with gas paid by `wallet` deducted from native."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for m in moves:
        out[m.asset_id] += m.amount
    for t in txs.values():
        if t.tx_from == wallet:
            out["native"] -= t.gas_fee
    return dict(out)
