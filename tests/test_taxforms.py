"""Tax-year assembly: Form 8949 boxes, Schedule D lines, capital loss carryover worksheet."""

from __future__ import annotations

from decimal import Decimal

from loledger.taxforms import Line, Profile, TaxYear, _schedule_d, box_for, carryover_out

D = Decimal


def test_boxes_by_year_and_broker() -> None:
    brokers = ["Gemini"]
    assert box_for("Gemini", "short", 2024, brokers) == "C" and box_for("Ethereum|0xa", "long", 2024, brokers) == "F"
    assert box_for("Gemini", "short", 2025, brokers) == "H" and box_for("Gemini", "long", 2025, brokers) == "K"
    assert box_for("MEXC", "short", 2025, brokers) == "I" and box_for("Solana|x", "long", 2026, brokers) == "L"


def test_carryover_worksheet_uses_the_3000_limit_and_short_term_first() -> None:
    assert carryover_out(D(-10000), D(2000), D(3000)) == (D(5000), D(0))  # 10k ST loss - 3k deduction - 2k LT gain
    assert carryover_out(D(1000), D(-6000), D(3000)) == (D(0), D(2000))  # LT loss 6k - 1k ST gain - 3k deduction
    assert carryover_out(D(500), D(100), D(3000)) == (D(0), D(0))


def _line(box: str, term: str, p: str, b: str) -> Line:
    return Line(box, term, "1 BTC", "01/01/2025", "02/01/2025", D(p), D(b), "acct", "BTC", D(1), "tx", 0)


def test_schedule_d_lines_from_8949_boxes_and_carryover() -> None:
    ty = TaxYear(2025, [_line("H", "short", "100", "40"), _line("I", "short", "10", "30"),
                        _line("L", "long", "50", "20")], {}, [], (D(500), D(0)), Profile())
    _schedule_d(ty)
    sd = ty.sched_d
    assert (sd["2h"], sd["3h"], sd["10h"], sd["6"]) == (D(60), D(-20), D(30), D(-500))
    assert (sd["7"], sd["15"], sd["16"], sd["21"]) == (D(-460), D(30), D(-430), D(-430))


def test_taxact_csv_in_7_and_6_column_layouts() -> None:
    from loledger.taxforms import taxact6_csv, taxact_csv

    ty = TaxYear(2025, [_line("I", "short", "100", "80"), _line("L", "long", "50", "70")], {}, [],
                 (Decimal(0), Decimal(0)), Profile())
    seven = taxact_csv(ty).decode().splitlines()
    six = taxact6_csv(ty).decode().splitlines()
    assert seven[0].endswith("Reporting Category,Long/Short") and seven[1].endswith(",I,Short")
    assert six[0].endswith("Reporting Category") and six[2].endswith(",L") and len(six) == len(seven)


def test_summary_totals_one_row_per_box_in_form_order_with_term_totals() -> None:
    from loledger.taxforms import box_summary, summary_totals_csv

    ty = TaxYear(2025, [_line("L", "long", "50", "70"), _line("I", "short", "100", "80"),
                        _line("H", "short", "10.004", "4"), _line("I", "short", "5", "1")], {}, [],
                 (Decimal(0), Decimal(0)), Profile())
    rows = box_summary(ty)
    assert [(r["box"], r["sched_d"], r["count"]) for r in rows] == [("H", "2", 1), ("I", "3", 2), ("L", "10", 1)]
    assert (rows[1]["proceeds"], rows[1]["basis"], rows[1]["gain"]) == (D(105), D(81), D(24))
    out = summary_totals_csv(ty).decode().splitlines()
    assert out[0].startswith("Form 8949 box,Term,Schedule D line,Transactions")
    assert out[1] == "H,Short,2,1,Digital assets - see attached statement,10.00,4.00,,0.00,6.00"
    assert out[-2] == ",Total short-term,,3,,115.00,85.00,,0.00,30.00"
    assert out[-1] == ",Total long-term,,1,,50.00,70.00,,0.00,-20.00"
    assert summary_totals_csv(TaxYear(2025, [], {}, [], (D(0), D(0)), Profile())) == b""


def _positions(pdf: object, page: int) -> dict[str, list[tuple[float, float]]]:
    """Text drawn on a page -> every (x, y) it is drawn at, from the content stream."""
    from io import BytesIO

    from pypdf import PdfReader

    buf = BytesIO()
    pdf.write(buf)  # type: ignore[attr-defined]
    out: dict[str, list[tuple[float, float]]] = {}

    def visit(text: str, cm: list[float], tm: list[float], *_: object) -> None:
        if text.strip():
            out.setdefault(text.strip(), []).append((cm[4] + tm[4], cm[5] + tm[5]))

    PdfReader(BytesIO(buf.getvalue())).pages[page].extract_text(visitor_text=visit)
    return out


def test_schedule_d_values_land_in_their_own_boxes_on_every_form_year() -> None:
    """The 2024 form has no (g) box on lines 1a/8a: mapping fields by order shifted every later value a box down."""
    from pathlib import Path

    import pytest

    from loledger.taxforms import _schedule_d, schedule_d_pdf

    years = [y for y in (2022, 2023, 2024, 2025) if Path(f"data/irs_forms/f1040sd--{y}.pdf").exists()]
    if not years:
        pytest.skip("blank IRS Schedule D forms not downloaded (data/irs_forms)")
    for y in years:
        box_s, box_l = ("C", "F") if y < 2025 else ("I", "L")
        ty = TaxYear(y, [_line(box_s, "short", "911.11", "122.22"), _line(box_l, "long", "633.33", "144.44")], {}, [],
                     (D(500), D(0)), Profile())
        _schedule_d(ty)
        pdf, _ = schedule_d_pdf(ty)
        pos = _positions(pdf, 0)
        (d3,), (e3,), (d10,) = pos["911"], pos["122"], pos["633"]  # line 3 proceeds, cost; line 10 (whole $)
        h3 = next(p for p in pos["789"] if abs(p[1] - d3[1]) < 3)  # line 3 gain on line 3's row (not line 4)
        assert d3[0] < e3[0] < h3[0], y
        assert any(abs(p[1] - d10[1]) < 3 for p in pos["489"]), y  # line 10 gain (line 15 repeats it)
        assert pos["500"][0][1] < d3[1] - 20, y  # the carryover is on line 6, below lines 4-5


def test_copies_of_one_form_page_each_show_only_their_own_values() -> None:
    """Form 8949 page 2 has a single content stream that every copy shares: merging one copy's values wrote them
    into all copies (the 2025 consolidated 8949 showed Box K and Box L overprinted on both pages)."""
    from io import BytesIO
    from pathlib import Path

    import pytest
    from pypdf import PdfReader

    from loledger.taxforms import _f8949_layout, _fill_pages

    tpl = Path("data/irs_forms/f8949--2025.pdf")
    if not tpl.exists():
        pytest.skip("blank IRS Form 8949 not downloaded (data/irs_forms)")
    first = _f8949_layout(tpl)[1]["rows"][0][0]  # page 2, row 1, column (a)
    w = _fill_pages(tpl, [(1, {first: "AAAA"}), (1, {first: "BBBB"}), (1, {})])
    buf = BytesIO()
    w.write(buf)
    pages = [p.extract_text() for p in PdfReader(BytesIO(buf.getvalue())).pages]
    assert "AAAA" in pages[0] and "BBBB" not in pages[0]
    assert "BBBB" in pages[1] and "AAAA" not in pages[1]
    assert all("Long-Term" in t for t in pages)  # every copy keeps the form itself, not only the values


def test_whole_dollars_and_readable_statement_accounts() -> None:
    from loledger.taxforms import _dollars, statement_account

    assert [_dollars(D(x)) for x in ("7204.83", "4250.49", "4250.50", "-5154.58", "-0.40", "0")] == \
        ["7,205", "4,250", "4,251", "(5,155)", "0", "0"]
    assert statement_account("escrow:cardano:00747b6953dfc") == "Cardano DEX order"
    assert statement_account("escrow:deeptrade:0x0d6ca894d") == "Sui DEX order (DeepTrade)"
    assert statement_account("Ethereum|0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "Ethereum wallet 0x5aAe…eAed"
    assert statement_account("Solana|ExampLeSo1anaWa11etAddressXyZ#rewards") == "Solana wallet ExampL…sXyZ"
    assert statement_account("Gemini") == "Gemini"


def test_whole_dollar_amounts_are_still_right_aligned() -> None:
    from loledger.taxforms import MONEY

    assert all(MONEY.match(v) for v in ("72,397", "(989)", "0", "1,234.56", "(5,155)"))
    assert not any(MONEY.match(v) for v in ("VARIOUS", "01/17/2025", "161.16 KAS", "See attached statement (61 lines)"))
