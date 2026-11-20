"""Fetch + normalize one EVM wallet, including chain-specific reconciliation passes."""

from __future__ import annotations

from ...model import Movement, TxContext
from .fetch import EvmFetcher, EvmRawBundle
from .normalize import native_deltas, normalize


def load_wallet(fetcher: EvmFetcher, wallet: str) -> tuple[EvmRawBundle, list[Movement], dict[str, TxContext]]:
    chain = fetcher.chain
    bundle = fetcher.fetch_wallet(wallet)
    moves, txs = normalize(chain, bundle)
    if chain.atomic_reconcile:
        bundle.atomic, bundle.block_ts = fetcher.atomic_changes(wallet, native_deltas(moves, txs, wallet))
        if bundle.atomic:
            moves, txs = normalize(chain, bundle)
    return bundle, moves, txs
