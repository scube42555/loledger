from __future__ import annotations

from datetime import date
from decimal import Decimal

from loledger.taxestimate import TABLES, crypto_part, estimate, extra_tax, income_tax, ordinary_tax, show_for
from loledger.taxforms import Profile, TaxYear

D = Decimal
MFJ = TABLES[2026]["married_joint"]


def _year(st: str, lt: str, income: str = "0", carry: tuple[str, str] = ("0", "0"),
          status: str = "married_joint") -> TaxYear:
    ty = TaxYear(2026, [], {"staking": D(income)} if D(income) else {}, [], (D(carry[0]), D(carry[1])),
                 Profile(filing_status=status))
    ty.sched_d = {"7": D(st) - D(carry[0]), "15": D(lt) - D(carry[1])}
    ty.sched_d["16"] = ty.sched_d["7"] + ty.sched_d["15"]
    return ty


def test_ordinary_brackets_2026_mfj() -> None:
    assert ordinary_tax(D(24800), MFJ) == D("2480.00")
    assert ordinary_tax(D(100800), MFJ) == D("2480") + D("76000") * D("0.12")


def test_long_term_gain_stacks_on_ordinary_income() -> None:
    # $90k ordinary + $20k LT: $8,900 at 0% (up to $98,900), $11,100 at 15%
    assert income_tax(D(110000), D(20000), MFJ) - ordinary_tax(D(90000), MFJ) == D("1665.00")


def test_short_term_gain_is_ordinary_and_carryover_applies_first() -> None:
    ty = _year("10000", "5000", carry=("4000", "0"))  # line 7 = 6,000, line 15 = 5,000
    c = crypto_part(ty, "married_joint")
    assert (c.ordinary, c.pref, c.gain) == (D(6000), D(5000), D(11000))
    # in the 22% bracket: 6,000 x 22% + 5,000 x 15%
    assert extra_tax(c, D(150000), MFJ) == D("2070.00")


def test_net_loss_saves_tax_at_the_marginal_rate_and_carries_the_rest() -> None:
    ty = _year("-10000", "2000")
    jt, sep = crypto_part(ty, "married_joint"), crypto_part(ty, "married_separate")
    assert (jt.deduction, jt.carry_out) == (D(3000), (D(5000), D0))
    assert (sep.deduction, sep.carry_out) == (D(1500), (D(6500), D0))
    assert extra_tax(jt, D(150000), MFJ) == D("-660.00")
    assert extra_tax(jt, -MFJ.std, MFJ) == D0  # nothing to offset


def test_niit_on_gains_above_the_threshold() -> None:
    c = crypto_part(_year("0", "10000"), "married_joint")
    other = D(250000) - MFJ.std - D(4000)  # MAGI ends 6,000 over the $250k threshold
    assert extra_tax(c, other, MFJ) == D("1500.00") + D("228.000")


def test_estimate_rows_and_when_it_shows() -> None:
    est = estimate(_year("1000", "0", income="500"), date(2026, 11, 23))
    assert est.ytd and est.status == "married_joint" and len(est.rows) == 8
    assert est.rows[0].tax["single"] == D0  # under the standard deduction
    assert est.rows[3].tax["married_joint"] == D("330.00")  # 22% bracket
    assert show_for(2026, date(2026, 11, 23)) and not show_for(2025, date(2026, 11, 23))
    assert show_for(2025, date(2026, 3, 1)) and not show_for(2024, date(2026, 3, 1))


D0 = Decimal(0)


def test_sciad_phases_out_with_agi_rounding_the_reduction_down() -> None:
    from loledger.taxestimate import sciad
    assert sciad(D(40000), "single") == D(15000)
    assert sciad(D(50000), "single") == D(12280)  # reduction 2,727.27 -> 2,720
    assert sciad(D(95000), "single") == D0
    assert sciad(D(80000), "married_joint") == D(30000) and sciad(D(190000), "married_joint") == D0


def test_sc_2026_deducts_44_percent_of_long_term_gain_above_the_sciad_phase_out() -> None:
    from loledger.taxestimate import SC, extra_state_tax
    c = crypto_part(_year("1000", "10000"), "married_joint")
    # AGI 250k: no SCIAD either way; +1,000 ST + 5,600 (56% of LT) taxed at 5.21%
    assert extra_state_tax(c, D(250000) - MFJ.std, "married_joint", MFJ, SC[2026]) == D("343.86")
    est = estimate(_year("1000", "10000", status="married_joint"), date(2026, 11, 23))
    assert est.state == "" and est.rows[1].state is None  # no state in the profile
    ty = _year("1000", "10000")
    ty.profile.state = "sc"
    est = estimate(ty, date(2026, 11, 23))
    assert est.state == "SC" and est.state_year == 2026
    assert est.rows[0].state is not None and est.rows[0].state["married_joint"] == D0  # under the SCIAD
