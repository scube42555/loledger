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
