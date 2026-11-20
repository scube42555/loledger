from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from txclass.adapters.evm.atomic import AtomicChange, find_atomic_changes

E18 = 10**18


def _actual(evm: dict[int, int], atomic: dict[int, int]) -> Callable[[int], int]:
    events = sorted({**{b: 0 for b in evm}, **{b: 0 for b in atomic}})

    def bal(b: int) -> int:
        return sum(evm.get(x, 0) + atomic.get(x, 0) for x in events if x <= b)
    return bal


def _run(evm: dict[int, int], atomic: dict[int, int], head: int = 1_000_000) -> list[AtomicChange]:
    calls: list[int] = []
    bal = _actual(evm, atomic)

    def actual(b: int) -> int:
        calls.append(b)
        return bal(b)
    deltas = {b: Decimal(v).scaleb(-18) for b, v in evm.items()}
    out = find_atomic_changes(deltas, actual, head, 18)
    assert len(calls) < 40 * (len(atomic) + 1) + len(evm) + 2
    return out


def test_no_atomic_activity_reconciles() -> None:
    assert _run({100: 5 * E18, 200: -2 * E18}, {}) == []


def test_export_between_evm_blocks_is_located_exactly() -> None:
    got = _run({100: 25 * E18, 900: E18}, {431: -25 * E18})
    assert got == [AtomicChange(431, Decimal(-25))]


def test_import_before_any_evm_activity_and_after_last() -> None:
    got = _run({5000: -E18}, {10: 3 * E18, 777_777: 2 * E18})
    assert got == [AtomicChange(10, Decimal(3)), AtomicChange(777_777, Decimal(2))]


def test_several_changes_in_one_interval_and_same_block_as_evm_tx() -> None:
    got = _run({100: 10 * E18, 900: -E18}, {300: -4 * E18, 301: E18, 900: 2 * E18})
    assert got == [AtomicChange(300, Decimal(-4)), AtomicChange(301, Decimal(1)), AtomicChange(900, Decimal(2))]
