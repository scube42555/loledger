"""Per-venue Cardano order datum decoders -> OrderTerms (raw integer units).

Field positions were verified on the user's own orders: each decoded min-receive is at or below
what the batcher actually paid out. Unknown shapes return None (the generic summary is kept).

  spectrum          C0[base, quote, poolNft, feeNum, exFeeNum, exFeeDen, rewardPkh, stakePkh?, baseAmount, minQuote]
  muesliswap-ob     C0[C0[creator, buyPolicy, buyName, sellPolicy, sellName, buyAmount, allowPartial, fee]]
  minswap-v1        C0[sender, receiver, receiverDatumHash?, step C0[desiredAsset, minReceive], batcherFee, outputAda]
  minswap-v2        C0[canceller, refundRcv, refundDatum, successRcv, successDatum, lpAsset,
                       step C0[direction, amountIn C0[n], minReceive, killable], maxBatcherFee, expiry C0[t]|C1[]]
  vyfi              C0[ownerPkh, C<n>[minReceive]]   (bought asset implied by the pool script)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ADA = "native"


@dataclass(frozen=True, slots=True)
class OrderTerms:
    venue: str
    sell_asset: str | None = None
    sell_amount: int | None = None
    buy_asset: str | None = None
    min_receive: int | None = None
    expiry_ms: int | None = None
    allow_partial: bool | None = None
    fee: int | None = None
    step: str = ""


def _f(v: Any) -> list[Any]:
    return v.get("fields", []) if isinstance(v, dict) else []


def _c(v: Any) -> int | None:
    return v.get("constructor") if isinstance(v, dict) else None


def _i(v: Any) -> int | None:
    return v.get("int") if isinstance(v, dict) and isinstance(v.get("int"), int) else None


def _b(v: Any) -> str | None:
    return v.get("bytes") if isinstance(v, dict) and isinstance(v.get("bytes"), str) else None


def _asset(policy: Any, name: Any) -> str | None:
    p, n = _b(policy), _b(name)
    if p is None or n is None:
        return None
    return ADA if p == "" else f"{p}.{n}"


def decode(venue: str, datum: Any) -> OrderTerms | None:
    try:
        if venue.startswith("spectrum"):
            f = _f(datum)
            if len(f) >= 10:
                return OrderTerms(venue, sell_asset=_asset(*_f(f[0])), sell_amount=_i(f[8]),
                                  buy_asset=_asset(*_f(f[1])), min_receive=_i(f[9]))
        if venue.startswith("muesliswap-orderbook"):
            f = _f(_f(datum)[0]) if _f(datum) else []
            if len(f) >= 8:
                return OrderTerms(venue, sell_asset=_asset(f[3], f[4]), buy_asset=_asset(f[1], f[2]),
                                  min_receive=_i(f[5]), allow_partial=_c(f[6]) == 1, fee=_i(f[7]))
        if venue.startswith("minswap-v1"):
            f = _f(datum)
            if len(f) >= 6 and _c(f[3]) == 0:
                step = _f(f[3])
                return OrderTerms(venue, buy_asset=_asset(*_f(step[0])), min_receive=_i(step[1]),
                                  fee=_i(f[4]), step="swap_exact_in")
        if venue.startswith("minswap-v2"):
            f = _f(datum)
            if len(f) >= 9 and _c(f[6]) == 0:
                step = _f(f[6])
                amount = _i(_f(step[1])[0]) if _f(step[1]) else None
                exp = _i(_f(f[8])[0]) if _c(f[8]) == 0 and _f(f[8]) else None
                return OrderTerms(venue, sell_amount=amount, min_receive=_i(step[2]), fee=_i(f[7]),
                                  expiry_ms=exp, step="swap_exact_in" + ("" if _c(step[0]) == 0 else "(b→a)"))
        if venue.startswith("vyfi"):
            f = _f(datum)
            if len(f) == 2 and _f(f[1]) and _i(_f(f[1])[0]) is not None:
                return OrderTerms(venue, min_receive=_i(_f(f[1])[0]), step=f"vyfi_op{_c(f[1])}")
    except (IndexError, TypeError):
        return None
    return None
