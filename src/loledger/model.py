"""Chain-agnostic data model shared by adapters, rules, decoders, matcher and reports."""

from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

# 2**256 has 78 digits; keep full precision for raw on-chain integers.
decimal.getcontext().prec = 100


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class AssetKind(StrEnum):
    NATIVE = "native"
    ERC20 = "erc20"
    ERC721 = "erc721"
    ERC1155 = "erc1155"
    CARDANO = "cardano_native"
    EXCHANGE = "exchange"  # balance on a centralized exchange account, asset_id = ticker


NATIVE_ASSET = "native"


@dataclass(frozen=True, slots=True)
class Movement:
    """One asset leg seen from one wallet's perspective. amount is signed: + in, - out."""

    chain: str
    tx_hash: str
    leg_id: str  # stable within (chain, tx_hash): "log:<idx>", "internal:<traceId>", "value", ...
    block: int
    timestamp: int
    wallet: str
    asset_id: str  # NATIVE_ASSET, contract address, "<contract>:<tokenId>", or "<policy>.<name>"
    asset_kind: AssetKind
    symbol: str
    decimals: int
    amount: Decimal
    counterparty: str
    source: str  # txlist | internal | tokentx | tokennfttx | token1155tx | utxo
    utxo_ref: str = ""
    datum_hash: str = ""

    @property
    def direction(self) -> Direction:
        return Direction.IN if self.amount >= 0 else Direction.OUT


@dataclass(frozen=True, slots=True)
class Log:
    address: str
    topics: tuple[str, ...]
    data: str
    log_index: int


@dataclass(slots=True)
class TxContext:
    """Transaction-level facts (not per wallet)."""

    chain: str
    tx_hash: str
    block: int
    timestamp: int
    tx_from: str
    tx_to: str
    method_id: str
    function_name: str
    is_error: bool
    gas_fee: Decimal  # in native units, paid by tx_from
    value: Decimal  # native value attached to the top-level call
    input: str = ""
    logs: list[Log] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # chain-specific extras (Cardano: msg, refs, datums)


def scale(raw: int | str, decimals: int) -> Decimal:
    return Decimal(int(raw)).scaleb(-decimals)
