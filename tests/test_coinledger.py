"""CoinLedger comparison: per-wallet tx-id matching, amount/time fallback, exchange daily nets."""

from __future__ import annotations

import csv
from pathlib import Path

from txclass.coinledger import load_coinledger, load_ours, report

CL_HDR = ["Internal Id", "Timestamp (UTC)", "Platform", "Account Name", "Type", "Record Type", "Asset", "Amount",
          "Blockchain Id", "Platform Id", "Description"]
OUR_HDR = ["chain", "timestamp", "tx_hash", "leg_id", "wallet", "wallet_label", "token", "amount", "usd_value_at_time",
           "classification", "subtype", "leg_role"]


def _w(p: Path, hdr: list[str], rows: list[list[str]]) -> Path:
    with p.open("w", newline="") as f:
        csv.writer(f).writerows([hdr, *rows])
    return p


def test_compare_matches_per_wallet_and_flags_differences(tmp_path: Path) -> None:
    cl = _w(tmp_path / "cl.csv", CL_HDR, [
        ["1", "2024-01-01T00:00:00", "Ethereum", "A", "Withdrawal", "Debit", "ETH", "1", "0xAB", "", ""],
        ["2", "2024-01-01T00:00:00", "Ethereum", "B", "Deposit", "Credit", "ETH", "1", "0xab", "", ""],
        ["3", "2024-01-02T00:00:00", "Ethereum", "B", "Deposit", "Credit", "MATIC", "5", "", "", ""],
        ["4", "2024-01-03T10:00:00", "Gemini", "Gemini", "Buy", "Credit", "BTC", "0.1", "", "9", ""],
        ["4", "2024-01-03T10:00:00", "Gemini", "Gemini", "Buy", "Debit", "USD", "4000", "", "9", ""],
    ])
    ours = _w(tmp_path / "ours.csv", OUR_HDR, [
        ["Ethereum", "1704067200", "0xab", "0", "0xa", "A", "ETH", "-1", "2000", "INTERNAL_TRANSFER", "", "out"],
        ["Ethereum", "1704067200", "0xab", "0", "0xb", "B", "ETH", "1", "2000", "INTERNAL_TRANSFER", "", "in"],
        ["Ethereum", "1704153700", "0xcd", "1", "0xb", "B", "POL", "5", "4", "TRANSFER", "from_eoa", "in"],
        ["Gemini", "1704276000", "gemini:order:1", "a", "g", "Gemini", "BTC", "0.1", "4000", "SWAP", "exchange_trade", "in"],
        ["Gemini", "1704276000", "gemini:order:1", "b", "g", "Gemini", "USD", "-3900", "3900", "SWAP", "exchange_trade", "out"],
    ])
    md, out = tmp_path / "r.md", tmp_path / "r.csv"
    stats = report(load_coinledger(cl), load_ours(ours), md, out)
    assert stats["tx_id"] == 2 and stats["amount_time"] == 1 and stats["amount_diffs"] == 0  # both sides of 0xab
    assert stats["exchange_day_diffs"] == 1  # USD -4000 vs -3900
    text = md.read_text()
    assert "Deposit | internal" in text and "Gemini" in text
