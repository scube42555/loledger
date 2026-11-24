"""Form 1099-DA CSV parsing and reconciliation against Form 8949 lines."""

from __future__ import annotations

from decimal import Decimal

from loledger.form1099da import Form, Row, _parse_stacked, parse_csv, reconcile, summary_totals
from loledger.taxforms import Line

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


def test_reconcile_compares_per_asset_per_day() -> None:
    form = Form("x", 2025, "Gemini", "f.csv", "csv", parse_csv(CSV))
    lines = [_ln("0.019 BTC", "BTC", "0.019", "01/18/2025", "1942.00"), _ln("1 BCH", "BCH", "1", "02/02/2025", "396.33"),
             _ln("3 ETH", "ETH", "3", "03/01/2025", "7500"), _ln("1 SOL", "SOL", "1", "03/05/2025", "150")]
    r = reconcile(form, lines)
    st = {(g.asset, g.day): g.status for g in r["groups"]}
    assert st == {("BTC", "2025-01-17"): "match", ("BCH", "2025-02-02"): "match",  # BTC paired across a day
                  ("ETH", "2025-03-01"): "units_differ", ("SOL", "2025-03-05"): "only_ours"}
    assert r["matched"] == 2 and [x.asset for x in r["unmatched_rows"]] == ["ETH"]
    assert [ln.asset for ln in r["unreported_lines"]] == ["SOL"]


def test_reconcile_sums_lot_rows_of_one_sale() -> None:
    rows = [Row("2025-03-11", "ETH", "0.1", "200.00", "150"), Row("2025-03-11", "ETH", "0.15", "300.00", "100")]
    r = reconcile(Form("x", 2025, "Gemini", "f.pdf", "pdf", rows), [_ln("0.25 ETH", "ETH", "0.25", "03/11/2025", "503")])
    (g,) = r["groups"]
    assert g.status == "proceeds_differ" and g.proceeds_diff == Decimal("-3") and g.da_basis == Decimal("250")


# one field per line, as pypdf extracts Gemini's 1099-DA (synthetic values)
STACKED = """Short-term transactions for which basis is not reported to the IRS 1,100.00 900.00 200.00 -
1,250.00 900.00 200.00 -
The section below summarizes any dispositions of noncovered digital assets resulting in a short-term capital gain/loss.
Name and Code of Digital Asset (1a, 1b) Check if proceeds from QOF (3b)
Number of
units (1c)
BITCOIN (BTC) - ABCDE1234
0.0105

Various

3/18/2025

890.89
X
729.46 -
-
- / -
-
0.002
8/5/2024
3/19/2025
209.11 170.54 -
-
- / -
-
Page 1 of 3
The section below summarizes any dispositions of digital assets resulting in a capital gain/loss on noncovered securities for which the holding period could not be determined
ETHEREUM (ETH) - X1Y2Z3
0.25
-
3/30/2025
150.00
X
- 0.25
3/30/2025
- / -
-
"""


def test_stacked_pdf_layout() -> None:
    rows = _parse_stacked(STACKED.splitlines())
    assert [(r.asset, r.qty, r.acquired, r.date_sold, r.proceeds, r.basis, r.term) for r in rows] == [
        ("BTC", "0.0105", "VARIOUS", "2025-03-18", "890.89", "729.46", "short"),
        ("BTC", "0.002", "2024-08-05", "2025-03-19", "209.11", "170.54", "short"),
        ("ETH", "0.25", "", "2025-03-30", "150.00", "", "")]
    assert not any(r.basis_reported for r in rows)
    assert summary_totals(STACKED) == Decimal("1250.00")


def test_apply_reported_proceeds(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import loledger.form1099da as m

    monkeypatch.setattr(m, "DATA_DIR", tmp_path)
    m.import_file(2025, "Gemini", "f.csv", CSV)
    lines = [_ln("0.019 BTC", "BTC", "0.019", "01/17/2025", "1950.00"),
             _ln("0.6 BCH", "BCH", "0.6", "02/02/2025", "240.00"), _ln("0.4 BCH", "BCH", "0.4", "02/02/2025", "160.00"),
             _ln("3 ETH", "ETH", "3", "03/01/2025", "7500"), _ln("1 BTC", "BTC", "1", "01/17/2025", "1", )]
    lines[4].account = "Coinbase"  # another broker's sale is left alone
    n, delta = m.apply_reported_proceeds(2025, lines)
    assert [ln.proceeds for ln in lines] == [Decimal("1942.39"), Decimal("237.80"), Decimal("158.53"),
                                             Decimal("7500"), Decimal("1")]  # ETH units differ: unchanged
    assert n == 3 and delta == Decimal("1942.39") + Decimal("396.33") - Decimal("2350")
    assert lines[0].own_proceeds == Decimal("1950.00") and lines[3].own_proceeds is None
    # reconciling afterwards still compares against our own figures
    g = next(g for g in m.reconcile(m.load_forms(2025)[0], lines)["groups"] if g.asset == "BTC")
    assert g.proceeds_diff == Decimal("-7.61")


def test_sales_the_1099da_does_not_list_move_to_box_i_or_l(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Gemini left a small USDC sale off its 1099-DA: it belongs in Box I (not reported to you), not H."""
    import loledger.form1099da as m

    monkeypatch.setattr(m, "DATA_DIR", tmp_path)
    m.import_file(2025, "Gemini", "f.csv", CSV)
    lines = [_ln("0.019 BTC", "BTC", "0.019", "01/17/2025", "1942.39"),   # on the form
             _ln("1434.94 USDC", "USDC", "1434.94", "07/18/2025", "1434.94"),  # not on it
             _ln("2 ETH", "ETH", "2", "03/02/2025", "5000.00")]  # dated a day later than the form: still on it
    lines[1].term = "short"
    long = _ln("5 SOL", "SOL", "5", "08/01/2025", "900")
    long.box, long.term = "K", "long"
    other = _ln("1 BTC", "BTC", "1", "05/05/2025", "1")
    other.account = "Coinbase"  # a broker with no imported form: unchanged
    assert m.move_unreported(2025, lines + [long, other]) == 2
    assert [ln.box for ln in lines] == ["H", "I", "H"] and long.box == "L" and other.box == "H"
