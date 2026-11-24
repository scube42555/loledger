"""`run --only <wallet>` fetches that wallet but must still treat the user's other wallets on the chain as theirs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from loledger import cli
from loledger.config import load_settings
from loledger.store import Store


def test_only_one_wallet_still_knows_the_other_wallets_on_its_chain(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "addresses.csv").write_text("Address,Chain,Label\nstake1mid,Cardano,Mid\nstake1air,Cardano,Air\n"
                                            "xch1aaa,Chia,A\nxch1bbb,Chia,B\n")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_fetch_cardano", lambda args, chain, ws, store, everyone=None: seen.update(
        cardano=[w.address for w in ws], everyone=[w.address for w in everyone or []]))
    monkeypatch.setattr(cli, "_fetch_chia", lambda chain, ws, store: seen.update(chia=[w.address for w in ws]))
    args = argparse.Namespace(addresses=str(tmp_path / "addresses.csv"), chain=None, only=["stake1mid", "xch1aaa"],
                              refresh=False)
    monkeypatch.chdir(tmp_path)  # no exports/ here: exchanges contribute nothing
    cli.cmd_fetch(args, load_settings(None), Store(":memory:"))
    assert seen["cardano"] == ["stake1mid"]  # only the selected wallet is fetched ...
    assert seen["everyone"] == ["stake1mid", "stake1air"]  # ... but its legs are attributed against all of them
    assert seen["chia"] == ["xch1aaa", "xch1bbb"]  # pooled spends pair across all Chia wallets: fetch them all
