"""Accounts page backend: reading/adding wallets in addresses.csv and the background sync runner."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from loledger.config import load_settings
from loledger.web import accounts as acc

S = load_settings(None)


def _csv(tmp_path: Path, body: str) -> Path:
    (tmp_path / "addresses.csv").write_text("Address,Chain,Label\n" + body)
    return tmp_path


def test_read_wallets_canonical_chain_gift_and_unsupported(tmp_path: Path) -> None:
    root = _csv(tmp_path, "0xAbC0000000000000000000000000000000000001,eth,Main\n"
                          "0xAbC0000000000000000000000000000000000002,Ethereum,Bob Gift\n"
                          "osmo1xyz,Osmosis,Osmo\n")
    ws = acc.read_wallets(root, S)
    assert [(w.chain, w.supported, w.gift) for w in ws] == [("Ethereum", True, False), ("Ethereum", True, True),
                                                           ("Osmosis", False, False)]
    assert ws[0].key == "Ethereum|0xabc0000000000000000000000000000000000001"


def test_validate_rejects_bad_duplicate_and_exchange(tmp_path: Path) -> None:
    root = _csv(tmp_path, "0xAbC0000000000000000000000000000000000001,Ethereum,Main\n")
    ws = acc.read_wallets(root, S)
    with pytest.raises(ValueError, match="42 characters"):
        acc.validate(S, "Ethereum", "0x12", ws)
    with pytest.raises(ValueError, match="already listed"):
        acc.validate(S, "ethereum", "0xabc0000000000000000000000000000000000001", ws)
    with pytest.raises(ValueError, match="exchange"):
        acc.validate(S, "Gemini", "whatever", ws)
    with pytest.raises(ValueError, match="addr1"):
        acc.validate(S, "Cardano", "Ae2abc", ws)
    with pytest.raises(ValueError, match="commas"):
        acc.validate(S, "Osmosis", "osmo1,x", ws)
    assert acc.validate(S, "base", "0xAbC0000000000000000000000000000000000001", ws) == (
        "Base", "0xAbC0000000000000000000000000000000000001")  # same address on another chain is fine
    assert acc.validate(S, "Osmosis", " osmo1abc ", ws) == ("Osmosis", "osmo1abc")  # unsupported chains allowed


def test_append_wallet_keeps_file_valid_without_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "addresses.csv").write_text("Address,Chain,Label\nbc1qa,Bitcoin,Cold")
    acc.append_wallet(tmp_path, "Solana", "So1ana", 'My "hot", wallet')
    ws = acc.read_wallets(tmp_path, S)
    assert [(w.address, w.chain, w.label) for w in ws] == [("bc1qa", "Bitcoin", "Cold"),
                                                           ("So1ana", "Solana", 'My "hot", wallet')]


def test_syncer_runs_steps_logs_output_and_reloads_on_success(tmp_path: Path) -> None:
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True))
    s._cli = lambda *a: [sys.executable, "-c", "print('fetched', 3)", *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet("bc1qa", "Bitcoin", "Cold", True, False, "utxo"))
    with pytest.raises(RuntimeError):
        s.sync_all()  # one job at a time
    for _ in range(100):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 0 and reloaded == [True]
    assert "fetched 3" in s.job.log and s.job.log[-1] == "Done."
    assert s.job.log[0].endswith("--refresh --only bc1qa")  # only this wallet is fetched


def test_syncer_stops_at_failing_step_without_reloading(tmp_path: Path) -> None:
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True))
    s._cli = lambda *a: [sys.executable, "-c", "import sys; sys.exit(3)", *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet("bc1qa", "Bitcoin", "", True, False, "utxo"))
    for _ in range(100):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 3 and not reloaded
    assert sum(line.startswith("$ loledger") for line in s.job.log) == 1  # second step never ran
