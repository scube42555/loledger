"""Avalanche C-Chain atomic import/export detection.

ImportTx/ExportTx (C <-> X/P chain) change a C-Chain address's AVAX balance without any EVM
transaction, so they never appear in txlist/txlistinternal. We find them by reconciliation:
compare the balance implied by our EVM movements with the explorer's historical balance
(`balancehistory`) on a grid of blocks (every block with wallet activity, plus the head), then
bisect each grid interval whose discrepancy changed down to the single block where it happened.

Limitation: an import and an export of identical size inside one grid interval cancel out and
are not detected (the balance reconciles either way).
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ...model import scale

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AtomicChange:
    block: int
    amount: Decimal  # + import into C-Chain, - export out of it (fee included)


def find_atomic_changes(
    deltas: dict[int, Decimal],
    actual_raw: Callable[[int], int],
    head: int,
    decimals: int,
) -> list[AtomicChange]:
    """deltas: native balance change per block from EVM data (incl. gas). actual_raw(b): balance (wei) at end of block b."""
    blocks = sorted(deltas)
    prefix: list[Decimal] = []
    run = Decimal(0)
    for b in blocks:
        run += deltas[b]
        prefix.append(run)

    def computed(b: int) -> Decimal:
        i = bisect.bisect_right(blocks, b)
        return prefix[i - 1] if i else Decimal(0)

    memo: dict[int, Decimal] = {}

    def diff(b: int) -> Decimal:
        if b not in memo:
            memo[b] = scale(actual_raw(b), decimals) - computed(b)
        return memo[b]

    changes: list[AtomicChange] = []

    def search(a: int, b: int) -> None:  # invariant: diff(a) != diff(b), a < b
        while b - a > 1:
            mid = (a + b) // 2
            if diff(mid) != diff(a):
                if diff(mid) != diff(b):
                    search(mid, b)  # changes on both sides
                b = mid
            else:
                a = mid
        changes.append(AtomicChange(block=b, amount=diff(b) - diff(a)))

    grid = sorted({0, *blocks, head})
    for a, b in zip(grid, grid[1:]):
        if diff(a) != diff(b):
            search(a, b)
    changes.sort(key=lambda c: c.block)
    return changes
