from __future__ import annotations

from typing import Any

import pytest

from loledger.adapters.evm import fetch
from loledger.adapters.evm.fetch import EvmFetcher, ExplorerError, check_explorer_body
from loledger.http import TransientError
from loledger.store import Store, canonical_params

from .fakes import CHAIN, OTHER, W, FakeExplorer, FakeRpc, token_row


def _rows(blocks: list[int]) -> list[dict[str, Any]]:
    # identical (hash, from, to, value) rows within a block are legitimate and must survive
    return [token_row(f"0x{b:064x}", b, OTHER, W, 5) for b in blocks]


def _fetcher(explorer: FakeExplorer, rpc: FakeRpc, store: Store, **kw: object) -> EvmFetcher:
    return EvmFetcher(CHAIN, store, explorer_http=explorer.http(), rpc_http=rpc.http(), **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize("blocks", [
    [1, 1, 2, 2, 2, 3, 4],          # page boundary splits block 2
    [1, 5, 5, 5, 5, 5, 5, 5, 9],    # one block bigger than a page -> single-block expansion
    [1, 2, 3],                      # exactly one full page then empty tail
    [],
])
def test_pagination_reads_every_row_exactly_once(monkeypatch: pytest.MonkeyPatch, blocks: list[int]) -> None:
    monkeypatch.setattr(fetch, "PAGE", 3)
    explorer = FakeExplorer({"tokentx": _rows(blocks)})
    f = _fetcher(explorer, FakeRpc(), Store(":memory:"))
    got = f.explorer.account_rows("tokentx", W)
    assert [int(str(r["blockNumber"])) for r in got] == blocks


def test_second_fetch_is_fully_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "PAGE", 3)
    rows = _rows([1, 2, 2, 3, 4])
    explorer = FakeExplorer({"tokentx": rows})
    rpc = FakeRpc({m: {str(r["hash"]): {"status": "0x1", "logs": []} for r in rows}
                   for m in ("eth_getTransactionReceipt", "eth_getTransactionByHash")})
    store = Store(":memory:")
    _fetcher(explorer, rpc, store).fetch_wallet(W)
    n_explorer, n_rpc = len(explorer.calls), rpc.calls
    store.stats.misses = 0
    _fetcher(explorer, rpc, store).fetch_wallet(W)
    assert (len(explorer.calls), rpc.calls) == (n_explorer, n_rpc)


def test_refresh_refetches_only_tail_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "PAGE", 3)
    data = {"tokentx": _rows([1, 2, 3, 4])}
    explorer = FakeExplorer(data)
    store = Store(":memory:")
    f = _fetcher(explorer, FakeRpc(), store)
    assert len(f.explorer.account_rows("tokentx", W)) == 4
    data["tokentx"] = _rows([1, 2, 3, 4, 7])  # new activity after the first fetch
    before = len(explorer.calls)
    assert len(_fetcher(explorer, FakeRpc(), store).explorer.account_rows("tokentx", W)) == 4  # cached tail
    refreshed = _fetcher(explorer, FakeRpc(), store, refresh_tail=True).explorer.account_rows("tokentx", W)
    assert len(refreshed) == 5
    # the old tail page [3,4] is re-requested (now full: [3,4,7]) plus one new tail page
    assert len(explorer.calls) - before == 2


def test_explorer_body_classification() -> None:
    check_explorer_body({"status": "1", "message": "OK", "result": []})
    check_explorer_body({"status": "0", "message": "No transactions found", "result": []})
    with pytest.raises(TransientError):
        check_explorer_body({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})
    with pytest.raises(ExplorerError):
        check_explorer_body({"status": "0", "message": "NOTOK", "result": "Invalid API Key"})


def test_api_key_is_never_part_of_cache_key() -> None:
    assert "apikey" not in canonical_params({"module": "account", "apikey": "SECRET"})
    assert canonical_params({"b": 1, "a": 2}) == canonical_params({"a": 2, "b": 1})
