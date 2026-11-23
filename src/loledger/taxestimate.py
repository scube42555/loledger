"""Rough federal tax estimate for a tax year's crypto activity, shown on the tax page for years not yet filed.

loledger only knows the crypto side of the return, so the estimate is the *extra* federal income tax the year's
crypto adds on top of everything else, worked out for each filing status and for other taxable income sitting at
the bottom of each ordinary bracket (plus "no other income", with the standard deduction). It follows the
Qualified Dividends and Capital Gain Tax Worksheet:
- Schedule D line 16 (after the loss carryover in) > 0: the net capital gain, min(line 15, line 16), is taxed at
  0/15/20% stacked on top of ordinary income. The rest of line 16 (net short-term gain) is ordinary.
- line 16 < 0: the loss offsets ordinary income up to $3,000 ($1,500 married filing separately). The rest
  carries forward.
- crypto income (staking, rewards, airdrops: Schedule 1) is ordinary.
- Net investment income tax: 3.8% of the net capital gain (short- and long-term) above the MAGI threshold.
  MAGI is approximated as taxable income plus the standard deduction.

South Carolina (`state: SC` in tax_profile.yaml), the same difference for the state return:
- 2026 on (Act 110 of 2026): SC taxable income = federal AGI - the SC Income Adjusted Deduction (SCIAD:
  $15,000 single / separate, $30,000 joint, phased out between $40k-$95k / $80k-$190k of federal AGI) - 44% of
  the net long-term capital gain (S.C. Code 12-6-1150, untouched by Act 110). Tax: 1.99% up to $30,000,
  5.21% above, for every filing status. Federal AGI is approximated as other taxable income plus the federal
  standard deduction.
- 2025: SC taxable income = federal taxable income - 44% of the net long-term gain; 0% / 3% / 6%.

Qualified dividends entered on the tax page (non-crypto) are on the return with and without the crypto: taxed
at 0/15/20% stacked below the crypto's long-term gain, counted in the NIIT, and ordinary income for SC.

Not included: credits, AMT, phase-outs other than the SCIAD, other capital gains or losses, and the cents
rounding the forms themselves use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

import yaml

from .taxforms import TaxYear, carryover_out

INPUTS = Path("data/tax_estimate.yaml")  # year -> {qualified_dividends: ...}, entered on the tax page

D0 = Decimal(0)
STATUSES = {"married_joint": "Married filing jointly", "married_separate": "Married filing separately",
            "single": "Single"}


@dataclass(frozen=True)
class Brackets:
    ordinary: tuple[tuple[Decimal, Decimal], ...]  # (lower edge of taxable income, rate)
    ltcg: tuple[Decimal, Decimal]  # taxable income where the 15% and the 20% rates start
    std: Decimal
    niit: Decimal  # MAGI threshold, not inflation-adjusted


RATES = (Decimal("0.10"), Decimal("0.12"), Decimal("0.22"), Decimal("0.24"), Decimal("0.32"), Decimal("0.35"),
         Decimal("0.37"))


def _b(edges: list[int], ltcg: tuple[int, int], std: int, niit: int) -> Brackets:
    return Brackets(tuple(zip([D0] + [Decimal(e) for e in edges], RATES, strict=True)),
                    (Decimal(ltcg[0]), Decimal(ltcg[1])), Decimal(std), Decimal(niit))


# Rev. Proc. 2024-40 as amended by P.L. 119-21 (2025 standard deduction), and Rev. Proc. 2025-32 (2026).
TABLES: dict[int, dict[str, Brackets]] = {
    2025: {
        "single": _b([11925, 48475, 103350, 197300, 250525, 626350], (48350, 533400), 15750, 200000),
        "married_joint": _b([23850, 96950, 206700, 394600, 501050, 751600], (96700, 600050), 31500, 250000),
        "married_separate": _b([11925, 48475, 103350, 197300, 250525, 375800], (48350, 300000), 15750, 125000),
    },
    2026: {
        "single": _b([12400, 50400, 105700, 201775, 256225, 640600], (49450, 545500), 16100, 200000),
        "married_joint": _b([24800, 100800, 211400, 403550, 512450, 768700], (98900, 613700), 32200, 250000),
        "married_separate": _b([12400, 50400, 105700, 201775, 256225, 384350], (49450, 306850), 16100, 125000),
    },
}


def table_year(year: int) -> int:
    """The bracket year used: the year itself, else the latest one known."""
    known = [y for y in TABLES if y <= year]
    return max(known) if known else min(TABLES)


def ordinary_tax(taxable: Decimal, b: Brackets) -> Decimal:
    return _bracket_tax(taxable, b.ordinary)


def income_tax(taxable: Decimal, pref: Decimal, b: Brackets) -> Decimal:
    """Tax on taxable income of which `pref` is net capital gain (Qualified Dividends and Capital Gain Tax
    Worksheet)."""
    taxable = max(taxable, D0)
    pref = min(max(pref, D0), taxable)
    ordinary = taxable - pref
    t15, t20 = b.ltcg
    at15 = max(D0, min(taxable, t20) - max(ordinary, t15))
    at20 = max(D0, taxable - max(ordinary, t20))
    return ordinary_tax(ordinary, b) + at15 * Decimal("0.15") + at20 * Decimal("0.20")


@dataclass
class Crypto:
    """What the year's crypto adds to the return."""

    ordinary: Decimal  # income + net short-term gain, or minus the deductible capital loss
    pref: Decimal  # net capital gain taxed at 0/15/20%
    deduction: Decimal  # capital loss deducted (positive)
    gain: Decimal  # net capital gain, short- and long-term (net investment income)
    carry_out: tuple[Decimal, Decimal]  # (ST, LT) loss carried into next year


def crypto_part(ty: TaxYear, status: str) -> Crypto:
    st, lt = ty.sched_d["7"], ty.sched_d["15"]
    net = st + lt
    limit = Decimal(1500) if status == "married_separate" else Decimal(3000)
    if net >= 0:
        pref = max(D0, min(lt, net))
        return Crypto(ty.income_total + net - pref, pref, D0, net, (D0, D0))
    ded = min(limit, -net)
    return Crypto(ty.income_total - ded, D0, ded, D0, carryover_out(st, lt, limit))


def _federal(taxable: Decimal, pref: Decimal, nii: Decimal, b: Brackets) -> Decimal:
    """Income tax plus NIIT (MAGI ~ taxable income + standard deduction)."""
    taxable = max(taxable, D0)
    return income_tax(taxable, pref, b) + Decimal("0.038") * max(D0, min(nii, taxable + b.std - b.niit))


def extra_tax(c: Crypto, other: Decimal, b: Brackets, qdiv: Decimal = D0) -> Decimal:
    """Federal tax the crypto adds when other ordinary taxable income is `other` (-std deduction = no other
    income) and there are `qdiv` qualified dividends besides."""
    base = _federal(other + qdiv, qdiv, qdiv, b)
    return _federal(other + qdiv + c.ordinary + c.pref, qdiv + c.pref, qdiv + max(c.gain, D0), b) - base


# ------------------------------------------------------------------------------------- South Carolina


@dataclass(frozen=True)
class StateRules:
    brackets: tuple[tuple[Decimal, Decimal], ...]  # (lower edge, rate), same for every filing status
    from_agi: bool  # start from federal AGI with the SCIAD (2026 on), else from federal taxable income
    ltcg_deduction: Decimal = Decimal("0.44")


SC_SCIAD = {"single": (15000, 40000, 55000), "married_separate": (15000, 40000, 55000),
            "married_joint": (30000, 80000, 110000)}  # amount, phase-out start, phase-out width
SC: dict[int, StateRules] = {
    2025: StateRules(((D0, D0), (Decimal(3560), Decimal("0.03")), (Decimal(17830), Decimal("0.06"))), False),
    2026: StateRules(((D0, Decimal("0.0199")), (Decimal(30000), Decimal("0.0521"))), True),
}


def sciad(agi: Decimal, status: str) -> Decimal:
    """SC Income Adjusted Deduction: the reduction is rounded down to $10 (S.C. Code 12-6-1140(15))."""
    amount, start, width = (Decimal(x) for x in SC_SCIAD[status])
    frac = (agi - start) / width
    if frac <= 0:
        return amount
    if frac >= 1:
        return D0
    return amount - (amount * frac / 10).to_integral_value(rounding=ROUND_FLOOR) * 10


def _bracket_tax(taxable: Decimal, brackets: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    tax = D0
    for i, (lo, rate) in enumerate(brackets):
        hi = brackets[i + 1][0] if i + 1 < len(brackets) else None
        if taxable <= lo:
            break
        tax += ((taxable if hi is None else min(taxable, hi)) - lo) * rate
    return tax


def _sc_tax(fed_taxable: Decimal, ltcg: Decimal, status: str, b: Brackets, r: StateRules) -> Decimal:
    if r.from_agi:
        agi = fed_taxable + b.std
        taxable = agi - sciad(agi, status) - r.ltcg_deduction * ltcg
    else:
        taxable = fed_taxable - r.ltcg_deduction * ltcg
    return _bracket_tax(max(taxable, D0), r.brackets)


def extra_state_tax(c: Crypto, other: Decimal, status: str, b: Brackets, r: StateRules,
                    qdiv: Decimal = D0) -> Decimal:
    """SC tax the crypto adds; `other` and `qdiv` as for extra_tax."""
    base = _sc_tax(other + qdiv, D0, status, b, r)
    return _sc_tax(other + qdiv + c.ordinary + c.pref, c.pref, status, b, r) - base


# ------------------------------------------------------------------------------------------- the table


@dataclass
class Row:
    label: str
    other: dict[str, Decimal | None]  # status -> other taxable income assumed
    tax: dict[str, Decimal]  # status -> extra federal tax
    rate: Decimal | None = None
    state: dict[str, Decimal] | None = None  # status -> extra state tax

    def total(self, status: str) -> Decimal:
        return self.tax[status] + (self.state[status] if self.state else D0)


@dataclass
class Estimate:
    year: int
    table_year: int
    status: str  # the profile's filing status (highlighted)
    crypto: dict[str, Crypto]
    rows: list[Row]
    carry_in: tuple[Decimal, Decimal]
    ytd: bool  # the year isn't over yet
    state: str = ""  # "SC" when estimated
    state_year: int = 0  # the SC rules year used
    qdiv: Decimal = D0  # qualified dividends assumed (non-crypto)


def show_for(year: int, today: date | None = None) -> bool:
    """Years whose return is not due yet: the current year, and last year until April 15."""
    today = today or date.today()
    return year >= today.year or (year == today.year - 1 and today < date(today.year, 4, 16))


def load_inputs(year: int, path: Path = INPUTS) -> dict[str, Decimal]:
    d = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    return {k: Decimal(str(v)) for k, v in (d.get(year) or {}).items()}


def save_input(year: int, key: str, value: Decimal, path: Path = INPUTS) -> None:
    d = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    y = d.setdefault(year, {})
    if value:
        y[key] = float(value) if value != value.to_integral_value() else int(value)
    else:
        y.pop(key, None)
        if not y:
            d.pop(year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(d, sort_keys=True))


def parse_amount(text: str) -> Decimal | None:
    """'$12,345.67' -> 12345.67; blank -> 0; anything else (or negative) -> None."""
    t = text.strip().replace("$", "").replace(",", "")
    if not t:
        return D0
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return v if v >= 0 and v.is_finite() else None


def estimate(ty: TaxYear, today: date | None = None, qdiv: Decimal = D0) -> Estimate:
    today = today or date.today()
    tyr = table_year(ty.year)
    tables = TABLES[tyr]
    crypto = {s: crypto_part(ty, s) for s in STATUSES}
    state = ty.profile.state.upper()
    syr = 0
    if state == "SC":
        known = [y for y in SC if y <= ty.year]
        syr = max(known) if known else min(SC)
    else:
        state = ""

    def row(label: str, other: dict[str, Decimal], shown: dict[str, Decimal | None], rate: Decimal | None) -> Row:
        fed = {s: extra_tax(crypto[s], other[s], tables[s], qdiv) for s in STATUSES}
        st = ({s: extra_state_tax(crypto[s], other[s], s, tables[s], SC[syr], qdiv) for s in STATUSES}
              if state else None)
        return Row(label, shown, fed, rate, st)

    rows = [row("No other income", {s: -tables[s].std for s in STATUSES}, {s: None for s in STATUSES}, None)]
    for i, rate in enumerate(RATES):
        other = {s: tables[s].ordinary[i][0] for s in STATUSES}
        rows.append(row(f"{rate * 100:.0f}% bracket", other, dict(other), rate))
    status = ty.profile.filing_status if ty.profile.filing_status in STATUSES else "single"
    return Estimate(ty.year, tyr, status, crypto, rows, ty.carry_in, ty.year >= today.year, state, syr, qdiv)
