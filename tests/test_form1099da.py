"""Form 1099-DA CSV parsing and reconciliation against Form 8949 lines."""

from __future__ import annotations

from decimal import Decimal

from txclass.form1099da import Form, parse_csv, reconcile
from txclass.taxforms import Line

CSV = b"""Form 1099-DA transaction detail,,,,,
Digital Asset Name,Number of Units,Date Acquired,Date Sold or Disposed,Gross Proceeds,Cost Basis,Basis Reported to IRS
BTC,0.019,Various,01/17/2025,"$1,942.39",,N
Bitcoin Cash (BCH),1.0,04/12/2024,02/02/2025,396.33,,N
ETH,2,,03/01/2025,"5,000.00",4000.00,Y
"""


def test_csv_header_detection_and_values() -> None:
    rows = parse_csv(CSV)
    assert [(r.date_sold, r.asset, r.qty, r.proceeds, r.basis_reported) for r in rows] == [
        ("2025-01-17", "BTC", "0.019", "1942.39", False), ("2025-02-02", "BCH", "1.0", "396.33", False),
        ("2025-03-01", "ETH", "2", "5000.00", True)]


def _ln(desc: str, asset: str, qty: str, sold: str, proceeds: str) -> Line:
    return Line("H", "short", desc, "VARIOUS", sold, Decimal(proceeds), Decimal(0), "Gemini", asset, Decimal(qty), "t", 0)


def test_reconcile_matches_by_asset_day_and_units() -> None:
    form = Form("x", 2025, "Gemini", "f.csv", "csv", parse_csv(CSV))
    lines = [_ln("0.019 BTC", "BTC", "0.019", "01/18/2025", "1942.00"), _ln("1 BCH", "BCH", "1", "02/02/2025", "396.33"),
             _ln("3 ETH", "ETH", "3", "03/01/2025", "7500")]
    r = reconcile(form, lines)
    assert r["matched"] == 2 and [x.asset for x in r["unmatched_rows"]] == ["ETH"]
    assert [ln.asset for ln in r["unreported_lines"]] == ["ETH"]
    assert r["matches"][0].proceeds_diff == Decimal("0.39")
