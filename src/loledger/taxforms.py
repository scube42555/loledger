"""Tax-year exports from the cost basis engine: IRS Form 8949 (detailed and consolidated), Schedule D,
Schedule 1, TaxAct / TurboTax import files, and CSV reports.

Forms are the official fillable IRS PDFs (downloaded once into data/irs_forms/, blank forms only), filled
locally with pypdf and flattened page by page. A year without its own forms yet (e.g. 2026 before the IRS
publishes them) uses the latest earlier year's forms; the README in the output folder says so.

Form 8949 boxes:
- through 2024: C (short-term) / F (long-term): digital assets were not reported on Form 1099-B.
- from 2025: H / K for sales through a US broker that issues Form 1099-DA (2025 forms report gross proceeds
  only, basis not reported); I / L for everything else (self-custody, non-US exchanges). Brokers are
  configurable in tax_profile.yaml; imported 1099-DA data will override this.
- futures/perpetual contract P&L (not digital assets themselves): C / F, one line per day per margin asset.

Lots of one disposal (same tx, account, asset, term, box) are combined into one line; their acquisition
date reads VARIOUS when they differ, as the 8949 instructions allow.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from .costbasis import Disposal, Result

D0 = Decimal(0)
FORMS_DIR = Path("data/irs_forms")
FORM_FILES = {"f8949": "f8949", "sd": "f1040sd", "s1": "f1040s1"}
DEFAULT_1099DA_BROKERS = ["Gemini", "Coinbase", "Crypto.com", "Binance.US"]
SHORT_BOXES = {"pre": "C", "broker": "H", "self": "I", "other": "C"}
LONG_BOXES = {"pre": "F", "broker": "K", "self": "L", "other": "F"}
BOX_ORDER_SHORT = {2024: "ABC", 2025: "ABCGHI"}  # checkbox order on page 1 (top to bottom)
BOX_ORDER_LONG = {2024: "DEF", 2025: "DEFJKL"}
SCHED_D_LINE = {"A": "1b", "G": "1b", "B": "2", "H": "2", "C": "3", "I": "3",
                "D": "8b", "J": "8b", "E": "9", "K": "9", "F": "10", "L": "10"}

log = logging.getLogger(__name__)


def _cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    d = _cents(d)
    return f"({-d:,.2f})" if d < 0 else f"{d:,.2f}"


def _mdy(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m/%d/%Y")


def _year(ts: int) -> int:
    return datetime.fromtimestamp(ts, UTC).year


def _qty(q: Decimal) -> str:
    s = f"{q:.8f}".rstrip("0").rstrip(".")
    return s or "0"


# ------------------------------------------------------------------------------------------ profile


@dataclass
class Profile:
    name: str = ""
    ssn: str = ""  # left blank unless the user fills it in tax_profile.yaml themselves
    filing_status: str = "single"  # married_separate -> $1,500 capital loss limit
    brokers_1099da: list[str] = field(default_factory=lambda: list(DEFAULT_1099DA_BROKERS))
    carryover: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)  # year -> (ST, LT) carried IN
    proceeds: str = "records"  # records | 1099da: use the proceeds a 1099-DA reports for the sales it covers

    @classmethod
    def load(cls, path: Path) -> Profile:
        if not path.exists():
            return cls()
        d = yaml.safe_load(path.read_text()) or {}
        co = {int(y): (Decimal(str(v.get("short_term", 0))), Decimal(str(v.get("long_term", 0))))
              for y, v in (d.get("capital_loss_carryover") or {}).items()}
        return cls(str(d.get("name") or ""), str(d.get("ssn") or ""), str(d.get("filing_status") or "single"),
                   list(d.get("brokers_1099da") or DEFAULT_1099DA_BROKERS), co,
                   "1099da" if str(d.get("proceeds") or "").lower().replace("-", "") == "1099da" else "records")

    @property
    def loss_limit(self) -> Decimal:
        return Decimal(1500) if self.filing_status == "married_separate" else Decimal(3000)


# --------------------------------------------------------------------------------------- the tax year


@dataclass
class Line:
    """One Form 8949 line."""

    box: str
    term: str
    description: str
    acquired: str  # mm/dd/yyyy or VARIOUS
    sold: str
    proceeds: Decimal
    basis: Decimal
    account: str
    asset: str
    qty: Decimal
    tx: str
    sold_ts: int
    own_proceeds: Decimal | None = None  # our computed proceeds, when replaced by the 1099-DA's

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.basis

    @property
    def records_proceeds(self) -> Decimal:
        return self.proceeds if self.own_proceeds is None else self.own_proceeds


@dataclass
class TaxYear:
    year: int
    lines: list[Line]
    income: dict[str, Decimal]  # type -> USD
    income_rows: list[Any]
    carry_in: tuple[Decimal, Decimal]
    profile: Profile
    sched_d: dict[str, Decimal] = field(default_factory=dict)
    positions: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    disposals: list[Disposal] = field(default_factory=list)
    prices: dict[str, Decimal] = field(default_factory=dict)  # Dec 31 USD price per asset family
    da_adjusted: tuple[int, Decimal] = (0, D0)  # lines given 1099-DA proceeds, net change in proceeds

    @property
    def income_total(self) -> Decimal:
        return sum(self.income.values(), D0)

    def box_totals(self) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
        t: dict[str, list[Decimal]] = defaultdict(lambda: [D0, D0, D0])
        for ln in self.lines:
            x = t[ln.box]
            x[0] += _cents(ln.proceeds)
            x[1] += _cents(ln.basis)
            x[2] += _cents(ln.proceeds) - _cents(ln.basis)
        return {k: (v[0], v[1], v[2]) for k, v in t.items()}


def box_for(d_acct: str, term: str, year: int, brokers: list[str], basis_reported: bool = False) -> str:
    table = SHORT_BOXES if term == "short" else LONG_BOXES
    if year <= 2024:
        return table["pre"]
    if d_acct in brokers and basis_reported:
        return "G" if term == "short" else "J"
    return table["broker"] if d_acct in brokers else table["self"]


def build_lines(res: Result, year: int, profile: Profile, da: dict[str, bool] | None = None) -> list[Line]:
    """da: accounts with an imported 1099-DA for the year -> whether it reports basis (G/J instead of H/K)."""
    da = da or {}
    brokers = list(profile.brokers_1099da) + list(da)
    groups: dict[tuple[str, str, str, str, str], list[Disposal]] = defaultdict(list)
    for d in res.disposals:
        if _year(d.ts) != year or d.lot.qty <= 0:
            continue
        box = box_for(d.acct, d.term, year, brokers, da.get(d.acct, False))
        groups[(d.tx, d.acct, d.fam, d.term, box)].append(d)
    lines = []
    for (tx, acct, fam, term, box), ds in groups.items():
        qty = sum((d.lot.qty for d in ds), D0)
        acqs = {_mdy(d.lot.acq) for d in ds}
        sym = fam.split("@")[0].upper()
        lines.append(Line(box, term, f"{_qty(qty)} {sym}", acqs.pop() if len(acqs) == 1 else "VARIOUS",
                          _mdy(ds[0].ts), sum((d.proceeds for d in ds), D0), sum((d.lot.cost for d in ds), D0),
                          acct, sym, qty, tx, ds[0].ts))
    # futures / perpetual contracts: one line per day and margin asset (not a digital asset disposal)
    fut: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for f in res.futures:
        if _year(f.ts) == year:
            fut[(_mdy(f.ts), f.fam)].append(f)
    for (day, fam), fs in fut.items():
        pnl = sum((f.usd for f in fs), D0)
        if not pnl:
            continue
        acct = fs[0].acct
        lines.append(Line("C", "short", f"{acct} futures P&L ({fam.upper()}-margined)", day, day,
                          max(pnl, D0), max(-pnl, D0), acct, f"{fam.upper()} futures", D0, "futures", fs[0].ts))
    lines.sort(key=lambda ln: (ln.box, ln.sold_ts, ln.description))
    return lines


def carryover_out(st: Decimal, lt: Decimal, limit: Decimal) -> tuple[Decimal, Decimal]:
    """IRS Capital Loss Carryover Worksheet, assuming taxable income large enough to absorb the deduction
    (lines 1-4 = the full limit). st/lt: this year's Schedule D lines 7 and 15. Returns (ST, LT) carried out
    as positive numbers."""
    net = st + lt
    if net >= 0:
        return D0, D0
    ded = min(limit, -net)
    st_loss, lt_gain = max(-st, D0), max(lt, D0)
    st_co = max(st_loss - (ded + lt_gain), D0)
    lt_loss, st_gain = max(-lt, D0), max(st, D0)
    lt_co = max(lt_loss - (st_gain + max(ded - st_loss, D0)), D0)
    return st_co, lt_co


def build_year(res: Result, year: int, profile: Profile, classified: Path | None = None) -> TaxYear:
    # capital loss carryover chain from the first year with activity (crypto-only estimate), unless given
    first = min([_year(d.ts) for d in res.disposals] + [year])
    carry = (D0, D0)
    ty: TaxYear | None = None
    for y in range(first, year + 1):
        carry = profile.carryover.get(y, carry)
        from .form1099da import apply_reported_proceeds, broker_accounts

        lines = build_lines(res, y, profile, broker_accounts(y))
        adj = apply_reported_proceeds(y, lines) if profile.proceeds == "1099da" else (0, D0)
        inc: dict[str, Decimal] = defaultdict(Decimal)
        rows = [x for x in res.income if _year(x.ts) == y]
        for x in rows:
            inc[x.typ] += x.usd
        ty = TaxYear(y, lines, dict(inc), rows, carry, profile, da_adjusted=adj)
        _schedule_d(ty)
        carry = carryover_out(ty.sched_d["7"], ty.sched_d["15"], profile.loss_limit)
    assert ty is not None
    ty.positions = res.year_end.get(year, {})
    ty.disposals = [d for d in res.disposals if _year(d.ts) == year]
    if classified is not None and classified.exists():
        ty.prices = year_end_prices(classified, year)
    return ty


def _schedule_d(ty: TaxYear) -> None:
    sd: dict[str, Decimal] = defaultdict(Decimal)
    for box, (p, b, g) in ty.box_totals().items():
        ln = SCHED_D_LINE[box]
        sd[f"{ln}d"] += p
        sd[f"{ln}e"] += b
        sd[f"{ln}h"] += g
    sd["6"] = -_cents(ty.carry_in[0])
    sd["14"] = -_cents(ty.carry_in[1])
    sd["7"] = sum((sd[f"{x}h"] for x in ("1a", "1b", "2", "3")), D0) + sd["4"] + sd["5"] + sd["6"]
    sd["15"] = sum((sd[f"{x}h"] for x in ("8a", "8b", "9", "10")), D0) + sd["11"] + sd["12"] + sd["13"] + sd["14"]
    sd["16"] = sd["7"] + sd["15"]
    if sd["16"] < 0:
        sd["21"] = -min(ty.profile.loss_limit, -sd["16"])
    ty.sched_d = dict(sd)


# ------------------------------------------------------------------------------------------- IRS PDFs


def form_path(form: str, year: int) -> tuple[Path, int]:
    """The blank IRS form for `year` (downloaded once), or the latest earlier year available."""
    FORMS_DIR.mkdir(parents=True, exist_ok=True)
    base = FORM_FILES[form]
    for y in range(year, 2019, -1):
        p = FORMS_DIR / f"{base}--{y}.pdf"
        if p.exists() and p.stat().st_size > 10000:
            return p, y
        try:
            url = f"https://www.irs.gov/pub/irs-prior/{base}--{y}.pdf"
            with urllib.request.urlopen(url, timeout=60) as r:  # blank public form only
                body = r.read()
            if body[:4] == b"%PDF":
                p.write_bytes(body)
                return p, y
        except Exception:
            continue
    raise FileNotFoundError(f"no IRS {base} form available for {year}")


def _fields(reader: Any, page: int) -> list[tuple[str, str, str]]:
    """(partial name, type, full name) of the page's fields, by their numeric index."""
    pref = f"Page{page + 1}[0]."
    out = []
    for full, f in reader.get_fields().items():
        if pref in full and f.get("/FT") in ("/Tx", "/Btn"):
            out.append((full.split(".")[-1], str(f.get("/FT")), full))

    def num(n: str) -> tuple[int, int]:
        m = re.match(r"[fc]\d+_(\d+)\[(\d+)\]", n)
        return (int(m.group(1)), int(m.group(2))) if m else (9999, 0)

    return sorted(out, key=lambda x: num(x[0]))


def _widgets(page: Any) -> dict[str, tuple[list[float], bool]]:
    """partial field name -> (rect, is_checkbox) for the page's form widgets."""
    out: dict[str, tuple[list[float], bool]] = {}
    for a in page.get("/Annots", []):
        a = a.get_object()
        if a.get("/Subtype") != "/Widget":
            continue
        par = a.get("/Parent")
        name = a.get("/T") or (par.get_object().get("/T") if par else None)
        ft = a.get("/FT") or (par.get_object().get("/FT") if par else None)
        if name is not None:
            out[str(name)] = ([float(x) for x in a["/Rect"]], ft == "/Btn")
    return out


MONEY = re.compile(r"^\(?-?[\d,]+\.\d\d\)?$")


def _fill_pages(template: Path, pages: list[tuple[int, dict[str, str]]]) -> Any:
    """One copy of template page `p` per entry with `values` (partial field names) drawn as a text layer at the
    fields' positions (money right-aligned, checkboxes as X). The blank page is shared, so 100s of pages stay
    small and fast; the output has no fillable fields left."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(str(template))
    geo = {p: _widgets(reader.pages[p]) for p in {p for p, _ in pages}}
    buf = BytesIO()
    c = canvas.Canvas(buf)
    for p, values in pages:
        box = reader.pages[p].mediabox
        c.setPageSize((float(box.width), float(box.height)))
        for name, val in values.items():
            g = geo[p].get(name)
            if g is None or not val:
                continue
            (x0, y0, x1, y1), is_box = g
            if is_box:
                c.setFont("Helvetica-Bold", 9)
                c.drawCentredString((x0 + x1) / 2, y0 + 1.5, "X")
                continue
            size = 8.0
            while size > 4.5 and stringWidth(val, "Helvetica", size) > (x1 - x0) - 3:
                size -= 0.5
            c.setFont("Helvetica", size)
            y = y0 + max((y1 - y0 - size) / 2, 0) + 1.5
            if MONEY.match(val):
                c.drawRightString(x1 - 2, y, val)
            else:
                c.drawString(x0 + 2, y, val)
        c.showPage()
    c.save()
    overlay = PdfReader(BytesIO(buf.getvalue()))
    out = PdfWriter()
    for i, (p, _) in enumerate(pages):
        page = out.add_page(reader.pages[p])
        if "/Annots" in page:
            del page["/Annots"]
        page.merge_page(overlay.pages[i])
    return out


def _f8949_layout(path: Path) -> dict[int, dict[str, Any]]:
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    r = PdfReader(str(path))
    lay = {}
    for page in (0, 1):
        fs = _fields(r, page)
        rows: dict[int, list[str]] = defaultdict(list)
        other = []
        boxes = []
        for name, ft, full in fs:
            m = re.search(r"Row(\d+)\[", full)
            if ft == "/Btn":
                boxes.append(name)
            elif m:
                rows[int(m.group(1))].append(name)
            else:
                other.append(name)
        states = {}
        for a in r.pages[page].get("/Annots", []):
            a = a.get_object()
            nm = a.get("/T") or a.get("/Parent").get_object().get("/T")
            if str(nm) in boxes:
                states[str(nm)] = [k for k in a["/AP"]["/N"].keys() if k != "/Off"][0]
        lay[page] = {"header": other[:2], "totals": other[-5:], "rows": [rows[k] for k in sorted(rows)],
                     "boxes": [(b, states.get(b, "/1")) for b in boxes]}
    return lay


def f8949_pdf(ty: TaxYear, consolidated: bool = False) -> tuple[Any, int]:
    path, used = form_path("f8949", ty.year)
    lay = _f8949_layout(path)
    order_s = BOX_ORDER_SHORT[2025 if used >= 2025 else 2024]
    order_l = BOX_ORDER_LONG[2025 if used >= 2025 else 2024]
    pages: list[tuple[int, dict[str, str]]] = []
    by_box: dict[str, list[Line]] = defaultdict(list)
    for ln in ty.lines:
        by_box[ln.box].append(ln)
    for box in sorted(by_box, key=lambda b: ("ABCGHIDEFJKL".index(b))):
        lines = by_box[box]
        page = 0 if box in order_s else 1
        order = order_s if page == 0 else order_l
        if box not in order:  # an older form without G-L: fall back to C/F
            box_on = "C" if page == 0 else "F"
        else:
            box_on = box
        L = lay[page]
        if consolidated:
            p_ = sum((_cents(x.proceeds) for x in lines), D0)  # same rounding as Schedule D
            b_ = sum((_cents(x.basis) for x in lines), D0)
            chunks = [[Line(box, lines[0].term, f"See attached statement ({len(lines)} lines)", "VARIOUS",
                            "VARIOUS", p_, b_, "", "", D0, "", 0)]]
        else:
            n = len(L["rows"])
            chunks = [lines[i:i + n] for i in range(0, len(lines), n)]
        for chunk in chunks:
            v: dict[str, str] = {}
            if ty.profile.name:
                v[L["header"][0]] = ty.profile.name
            if ty.profile.ssn:
                v[L["header"][1]] = ty.profile.ssn
            name, state = L["boxes"][order.index(box_on)]
            v[name] = state
            for row, ln in zip(L["rows"], chunk):
                a, b, c, d, e, f, g, h = row[:8]
                v.update({a: ln.description[:40], b: ln.acquired, c: ln.sold, d: _money(ln.proceeds),
                          e: _money(ln.basis), h: _money(_cents(ln.proceeds) - _cents(ln.basis))})
            tp = sum((_cents(x.proceeds) for x in chunk), D0)
            tb = sum((_cents(x.basis) for x in chunk), D0)
            td, te, _tf, _tg, th = L["totals"]
            v.update({td: _money(tp), te: _money(tb), th: _money(tp - tb)})
            pages.append((page, v))
    if not pages:
        return None, used
    return _fill_pages(path, pages), used


def statement_pdf(ty: TaxYear, out: Any) -> None:
    """The statement attached to the consolidated Form 8949: every line, grouped by box, with subtotals."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    doc = SimpleDocTemplate(out, pagesize=landscape(letter), leftMargin=30, rightMargin=30, topMargin=30,
                            bottomMargin=30, title=f"Form 8949 statement {ty.year}")
    story: list[Any] = [Paragraph(f"Attachment to Form 8949, tax year {ty.year}"
                                  + (f" &mdash; {ty.profile.name}" if ty.profile.name else ""), st["Title"])]
    by_box: dict[str, list[Line]] = defaultdict(list)
    for ln in ty.lines:
        by_box[ln.box].append(ln)
    for box in sorted(by_box, key=lambda b: "ABCGHIDEFJKL".index(b)):
        lines = by_box[box]
        story += [Paragraph(f"Box {box} &mdash; {'short' if lines[0].term == 'short' else 'long'}-term "
                            f"({len(lines)} lines)", st["Heading2"])]
        data = [["(a) Description", "Account", "(b) Acquired", "(c) Sold", "(d) Proceeds", "(e) Cost basis",
                 "(h) Gain or (loss)"]]
        for ln in lines:
            data.append([ln.description[:48], ln.account[:28], ln.acquired, ln.sold, _money(ln.proceeds),
                         _money(ln.basis), _money(_cents(ln.proceeds) - _cents(ln.basis))])
        p_ = sum((_cents(x.proceeds) for x in lines), D0)
        b_ = sum((_cents(x.basis) for x in lines), D0)
        data.append(["Totals", "", "", "", _money(p_), _money(b_), _money(p_ - b_)])
        t = Table(data, repeatRows=1, colWidths=[190, 140, 70, 70, 85, 85, 90])
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
                               ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
        story += [t, Spacer(1, 12)]
    doc.build(story)


def schedule_d_pdf(ty: TaxYear) -> tuple[Any, int]:
    from pypdf import PdfReader

    path, used = form_path("sd", ty.year)
    r = PdfReader(str(path))
    p1 = [n for n, ft, _ in _fields(r, 0) if ft == "/Tx"]
    p2 = [n for n, ft, _ in _fields(r, 1) if ft == "/Tx"]
    keys = ["name", "ssn"]
    for ln in ("1a", "1b", "2", "3"):
        keys += [f"{ln}d", f"{ln}e", f"{ln}g", f"{ln}h"]
    keys += ["4", "5", "6", "7"]
    for ln in ("8a", "8b", "9", "10"):
        keys += [f"{ln}d", f"{ln}e", f"{ln}g", f"{ln}h"]
    keys += ["11", "12", "13", "14", "15"]
    m1 = dict(zip(keys, p1))
    m2 = dict(zip(["16", "18", "19", "21"], p2))
    sd = ty.sched_d
    v1: dict[str, str] = {}
    if ty.profile.name:
        v1[m1["name"]] = ty.profile.name
    if ty.profile.ssn:
        v1[m1["ssn"]] = ty.profile.ssn
    for k, fld in m1.items():
        if k not in ("name", "ssn") and sd.get(k):  # lines 6 and 14 print their own ( ): enter the amount
            v1[fld] = _money(-sd[k]) if k in ("6", "14") else _money(sd[k])
    v2 = {m2["16"]: _money(sd["16"])} if sd.get("16") else {}
    if sd.get("21"):
        v2[m2["21"]] = _money(-sd["21"])  # printed ( ) on the form
    return _fill_pages(path, [(0, v1), (1, v2)]), used


S1_MAP = {  # line -> partial field name, per form year
    2022: {"8z_desc": "f1_33[0]", "8z": "f1_34[0]", "9": "f1_35[0]", "10": "f1_36[0]"},
    2023: {"8z_desc": "f1_33[0]", "8z": "f1_34[0]", "9": "f1_35[0]", "10": "f1_36[0]"},
    2024: {"8v": "f1_33[0]", "8z_desc": "f1_35[0]", "8z": "f1_36[0]", "9": "f1_37[0]", "10": "f1_38[0]"},
    2025: {"8v": "f1_34[0]", "8z_desc": "f1_35[0]", "8z": "f1_36[0]", "9": "f1_37[0]", "10": "f1_38[0]"},
}


def schedule_1_pdf(ty: TaxYear) -> tuple[Any, int]:
    from pypdf import PdfReader

    path, used = form_path("s1", ty.year)
    r = PdfReader(str(path))
    head = [n for n, ft, _ in _fields(r, 0) if ft == "/Tx"][:2]
    m = S1_MAP[min(max(S1_MAP), max(min(S1_MAP), used))]
    amt = _money(ty.income_total)
    v: dict[str, str] = {}
    if ty.profile.name:
        v[head[0]] = ty.profile.name
    if ty.profile.ssn:
        v[head[1]] = ty.profile.ssn
    if "8v" in m:
        v[m["8v"]] = amt
    else:
        v[m["8z_desc"]] = "Digital assets received as ordinary income"
        v[m["8z"]] = amt
    v[m["9"]] = amt
    v[m["10"]] = amt
    return _fill_pages(path, [(0, v)]), used


# --------------------------------------------------------------------------------- software and CSVs


def _csv_text(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode()


def taxact_csv(ty: TaxYear, term_column: bool = True) -> bytes:
    """TaxAct crypto import. term_column=False gives the 6-column layout CoinLedger exports for TaxAct (the term
    is implied by the reporting category); the 7th 'Long/Short' column suits importers that map columns."""
    head = ["Description", "Date Acquired", "Date Sold", "Sales Proceeds", "Cost or Other Basis", "Reporting Category"]
    return _csv_text(head + (["Long/Short"] if term_column else []),
                     [[ln.description, "Various" if ln.acquired == "VARIOUS" else ln.acquired, ln.sold,
                       f"{_cents(ln.proceeds):.2f}", f"{_cents(ln.basis):.2f}", ln.box]
                      + (["Short" if ln.term == "short" else "Long"] if term_column else []) for ln in ty.lines])


def taxact6_csv(ty: TaxYear) -> bytes:
    return taxact_csv(ty, term_column=False)


def taxact_summary_csv(ty: TaxYear) -> bytes:
    return taxact_csv(TaxYear(ty.year, summary_lines(ty), ty.income, ty.income_rows, ty.carry_in, ty.profile))


def summary_lines(ty: TaxYear) -> list[Line]:
    """One line per (box, asset): for importers with row limits (TaxAct: 2,000)."""
    g: dict[tuple[str, str, str], list[Line]] = defaultdict(list)
    for ln in ty.lines:
        g[(ln.box, ln.term, ln.asset)].append(ln)
    out = []
    for (box, term, asset), ls in sorted(g.items()):
        qty = sum((x.qty for x in ls), D0)
        out.append(Line(box, term, f"{_qty(qty)} {asset}" if qty else asset, "VARIOUS", "VARIOUS",
                        sum((_cents(x.proceeds) for x in ls), D0), sum((_cents(x.basis) for x in ls), D0), "",
                        asset, qty, "", 0))
    return out


BOX_ORDER = "ABCGHIDEFJKL"


def box_summary(ty: TaxYear) -> list[dict[str, Any]]:
    """Form 8949 summary totals, one row per box in form order: what you type into software that takes totals
    (Cash App Taxes, Free File Fillable Forms) with the line-by-line statement sent separately (Form 8453)."""
    counts: dict[str, int] = defaultdict(int)
    for ln in ty.lines:
        counts[ln.box] += 1
    return [{"box": box, "term": "short" if box in "ABCGHI" else "long", "sched_d": SCHED_D_LINE[box],
             "count": counts[box], "proceeds": p, "basis": b, "adjustment": D0, "gain": g}
            for box, (p, b, g) in sorted(ty.box_totals().items(), key=lambda kv: BOX_ORDER.index(kv[0]))]


def summary_totals_csv(ty: TaxYear) -> bytes:
    rows = box_summary(ty)
    if not rows:
        return b""
    desc = "Digital assets - see attached statement" if ty.year >= 2025 else "Cryptocurrency - see attached statement"
    out = [[r["box"], "Short" if r["term"] == "short" else "Long", r["sched_d"], str(r["count"]), desc,
            f"{r['proceeds']:.2f}", f"{r['basis']:.2f}", "", f"{r['adjustment']:.2f}", f"{r['gain']:.2f}"]
           for r in rows]
    for term, lab in (("short", "Total short-term"), ("long", "Total long-term")):
        rs = [r for r in rows if r["term"] == term]
        if rs:
            out.append(["", lab, "", str(sum(r["count"] for r in rs)), "",
                        f"{sum((r['proceeds'] for r in rs), D0):.2f}", f"{sum((r['basis'] for r in rs), D0):.2f}", "",
                        f"{sum((r['adjustment'] for r in rs), D0):.2f}", f"{sum((r['gain'] for r in rs), D0):.2f}"])
    return _csv_text(["Form 8949 box", "Term", "Schedule D line", "Transactions", "Description (a)",
                      "Proceeds (d)", "Cost or other basis (e)", "Adjustment code (f)", "Adjustment amount (g)",
                      "Gain or loss (h)"], out)


def turbotax_csv(ty: TaxYear) -> bytes:
    return _csv_text(["Name of digital asset", "Received date", "Cost or other basis", "Date sold", "Proceeds",
                      "Number of units", "Applicable checkbox on Form 8949"],
                     [[ln.asset, ln.acquired, f"{_cents(ln.basis):.2f}", ln.sold, f"{_cents(ln.proceeds):.2f}",
                       _qty(ln.qty) if ln.qty else "", ln.box] for ln in ty.lines])


def capital_gains_csv(ty: TaxYear) -> bytes:
    return _csv_text(["form_8949_box", "term", "description", "account", "asset", "quantity", "date_acquired",
                      "date_sold", "proceeds_usd", "cost_basis_usd", "gain_usd", "tx"],
                     [[ln.box, ln.term, ln.description, ln.account, ln.asset, _qty(ln.qty), ln.acquired, ln.sold,
                       f"{_cents(ln.proceeds):.2f}", f"{_cents(ln.basis):.2f}",
                       f"{_cents(ln.proceeds) - _cents(ln.basis):.2f}", ln.tx] for ln in ty.lines])


def income_csv(ty: TaxYear) -> bytes:
    return _csv_text(["date", "account", "asset", "quantity", "fmv_usd", "type", "tx"],
                     [[datetime.fromtimestamp(x.ts, UTC).strftime("%Y-%m-%d %H:%M:%S"), x.acct, x.fam.upper(),
                       _qty(x.qty), f"{x.usd:.2f}", x.typ, x.tx] for x in sorted(ty.income_rows, key=lambda x: x.ts)])


def positions_csv(ty: TaxYear) -> bytes:
    rows = []
    for (acct, fam), lots in sorted(ty.positions.items()):
        if "@" in fam:
            continue
        px = ty.prices.get(fam)
        for lot in lots:
            rows.append([acct, fam.upper(), _qty(lot.qty), f"{lot.cost:.2f}",
                         datetime.fromtimestamp(lot.acq, UTC).strftime("%Y-%m-%d"), "" if px is None else f"{px:f}",
                         "" if px is None else f"{lot.qty * px:.2f}", lot.origin])
    return _csv_text(["account", "asset", "quantity", "cost_basis_usd", "date_acquired", "price_usd_dec31",
                      "market_value_usd", "lot_origin"], rows)


def audit_csv(ty: TaxYear) -> bytes:
    return _csv_text(["date_sold_utc", "account", "asset", "quantity", "proceeds_usd", "cost_basis_usd", "gain_usd",
                      "term", "date_acquired_utc", "event", "lot_method", "tx", "lot_origin", "lot_flag"],
                     [[datetime.fromtimestamp(d.ts, UTC).strftime("%Y-%m-%d %H:%M:%S"), d.acct, d.fam.upper(),
                       _qty(d.lot.qty), f"{d.proceeds:.6f}", f"{d.lot.cost:.6f}", f"{d.gain:.6f}", d.term,
                       datetime.fromtimestamp(d.lot.acq, UTC).strftime("%Y-%m-%d %H:%M:%S"), d.event, d.method, d.tx,
                       d.lot.origin, d.lot.flag] for d in sorted(ty.disposals, key=lambda d: d.ts)])


def year_end_prices(classified: Path, year: int) -> dict[str, Decimal]:
    """Last USD price seen on or before Dec 31 of `year` per asset family (from classified.csv)."""
    from .reconcile import family

    end = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp())
    best: dict[str, tuple[int, Decimal]] = {}
    with open(classified, newline="") as f:
        for r in csv.DictReader(f):
            if not r["usd_price"] or r["subtype"].startswith("spam"):
                continue
            ts = int(r["timestamp"])
            if ts >= end:
                continue
            fam = family(r["token"])
            if fam not in best or ts >= best[fam][0]:
                best[fam] = (ts, Decimal(r["usd_price"]))
    return {k: v[1] for k, v in best.items()}


def _pdf_bytes(w: Any) -> bytes:
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def f8949_bytes(ty: TaxYear) -> bytes:
    w, _ = f8949_pdf(ty)
    return _pdf_bytes(w) if w is not None else b""


def f8949_consolidated_bytes(ty: TaxYear) -> bytes:
    from pypdf import PdfReader, PdfWriter

    wc, _ = f8949_pdf(ty, consolidated=True)
    if wc is None:
        return b""
    st = io.BytesIO()
    statement_pdf(ty, st)
    merged = PdfWriter()
    for p in wc.pages:
        merged.add_page(p)
    for p in PdfReader(io.BytesIO(st.getvalue())).pages:
        merged.add_page(p)
    return _pdf_bytes(merged)


@dataclass(frozen=True)
class Export:
    key: str
    tab: str  # irs | software | csv
    title: str
    description: str
    filename: str  # with {year}
    make: Any  # Callable[[TaxYear], bytes]


EXPORTS = [
    Export("f8949", "irs", "IRS Form 8949", "Every short- and long-term disposal for the year, line by line.",
           "form_8949_{year}.pdf", f8949_bytes),
    Export("f8949c", "irs", "IRS Form 8949 (consolidated)", "One summary line per box, with the detailed "
           "statement attached.", "form_8949_consolidated_{year}.pdf", f8949_consolidated_bytes),
    Export("schd", "irs", "IRS Schedule D", "Short- and long-term totals, carryovers and the net result.",
           "schedule_d_{year}.pdf", lambda ty: _pdf_bytes(schedule_d_pdf(ty)[0])),
    Export("sch1", "irs", "IRS Schedule 1", "Crypto received as ordinary income (staking, farming, rewards).",
           "schedule_1_{year}.pdf", lambda ty: _pdf_bytes(schedule_1_pdf(ty)[0])),
    Export("taxact", "software", "TaxAct (CSV)", "Gains and losses for TaxAct's crypto CSV import (max 2,000 "
           "lines).", "taxact_{year}.csv", taxact_csv),
    Export("taxact6", "software", "TaxAct (CSV, 6 columns)", "The same lines in the 6-column layout CoinLedger "
           "exports for TaxAct (no Long/Short column; the term follows from the reporting category).",
           "taxact_{year}_6col.csv", taxact6_csv),
    Export("taxact_sum", "software", "TaxAct summary (CSV)", "One line per Form 8949 box and asset, for years over "
           "TaxAct's 2,000-line limit; attach the consolidated 8949.", "taxact_{year}_summary.csv", taxact_summary_csv),
    Export("f8949_totals", "software", "Form 8949 summary totals (CSV)", "One row per Form 8949 box with "
           "proceeds, basis and gain, for software where you enter totals instead of importing every sale (Cash "
           "App Taxes, Free File Fillable Forms). Mail the consolidated 8949's statement with Form 8453.",
           "form_8949_summary_totals_{year}.csv", summary_totals_csv),
    Export("turbotax", "software", "TurboTax (CSV)", "Digital-asset gains and losses in the 7-column statement "
           "layout.", "turbotax_{year}.csv", turbotax_csv),
    Export("gains", "csv", "Capital gains", "Proceeds, cost basis and gain for every Form 8949 line.",
           "capital_gains_{year}.csv", capital_gains_csv),
    Export("income", "csv", "Income", "Fair market value of every income receipt.", "income_{year}.csv", income_csv),
    Export("positions", "csv", "End-of-year positions", "Holdings on Dec 31 with each lot's basis and acquisition "
           "date.", "end_of_year_positions_{year}.csv", positions_csv),
    Export("audit", "csv", "Audit trail", "Every lot used in every disposal: dates, method, source transaction.",
           "audit_trail_{year}.csv", audit_csv),
]


def year_notes(ty: TaxYear) -> list[str]:
    notes = []
    _, used = form_path("f8949", ty.year)
    if used != ty.year:
        notes.append(f"IRS forms for {ty.year} are not published yet: the {used} forms are used until they are.")
    n, delta = ty.da_adjusted
    if n:
        notes.append(f"Proceeds from Form 1099-DA: {n} Form 8949 line(s) use the proceeds your broker reported "
                     f"({'+' if delta >= 0 else '-'}${abs(delta):,.2f} vs. your records). Lot-level CSV reports keep your "
                     "own figures.")
    if len(ty.lines) > 2000:
        notes.append(f"{len(ty.lines)} Form 8949 lines: more than TaxAct's 2,000-line import limit; use the TaxAct "
                     "summary and attach the consolidated Form 8949.")
    return notes


# --------------------------------------------------------------------------------------------- driver


def export_year(res: Result, year: int, out_dir: Path, profile: Profile, classified: Path) -> dict[str, Any]:
    """Write every export for `year` (the CLI shortcut; the web app generates them one at a time)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ty = build_year(res, year, profile, classified)
    written = []
    for e in EXPORTS:
        if e.key == "taxact_sum" and len(ty.lines) <= 2000:
            continue
        body = e.make(ty)
        if body:
            name = e.filename.format(year=year)
            (out_dir / name).write_bytes(body)
            written.append(name)
    notes = year_notes(ty)
    _readme(ty, out_dir, notes, written)
    written.append("README.md")
    return {"year": year, "lines": len(ty.lines), "files": written, "notes": notes, "sched_d": ty.sched_d,
            "income": ty.income_total}


def _readme(ty: TaxYear, out_dir: Path, notes: list[str], written: list[str]) -> None:
    sd = ty.sched_d
    tot = ty.box_totals()
    m = ty.profile
    L = [f"# Tax year {ty.year}: crypto exports", "",
         "Generated from this project's own cost basis engine (classified.csv + manual.yaml). Not tax advice: "
         "review with a tax professional before filing.", "",
         "## Method", "",
         "- Through 2024: HIFO across all wallets and accounts (universal), as filed for 2022-2024.",
         "- From 2025-01-01: HIFO within each wallet / exchange account (standing order), after the Rev. Proc. "
         "2024-28 allocation of unused basis on 2025-01-01. Keep `method_statement.txt` with your records.",
         "- Fees paid in crypto are small disposals at FMV; their value is added to the cost of what the same "
         "transaction bought (or deducted from its proceeds).", "",
         "## Form 8949 boxes", ""]
    for box, (p, b, g) in sorted(tot.items(), key=lambda kv: BOX_ORDER.index(kv[0])):
        n = sum(1 for ln in ty.lines if ln.box == box)
        L.append(f"- Box {box}: {n} lines, proceeds ${p:,.2f}, basis ${b:,.2f}, gain ${g:,.2f} "
                 f"(Schedule D line {SCHED_D_LINE[box]})")
    L += ["", "## Schedule D", "",
          f"- Line 7 (net short-term): ${sd['7']:,.2f}" + (f" (includes a ${-sd['6']:,.2f} short-term carryover)"
                                                             if sd.get("6") else ""),
          f"- Line 15 (net long-term): ${sd['15']:,.2f}" + (f" (includes a ${-sd['14']:,.2f} long-term carryover)"
                                                             if sd.get("14") else ""),
          f"- Line 16: ${sd['16']:,.2f}" + (f"; line 21 deductible loss ${-sd['21']:,.2f} (limit "
                                            f"${m.loss_limit:,.0f})" if sd.get("21") else ""),
          "- Carryovers are a crypto-only estimate chained from the first year (IRS carryover worksheet, assuming "
          "enough taxable income); put the amounts from your actual returns in tax_profile.yaml "
          "(capital_loss_carryover) to override.",
          "- Lines 17-22 questions and the tax computation are left to your tax software / preparer.", "",
          "## Schedule 1", "",
          f"- Digital-asset income: ${ty.income_total:,.2f} "
          + ("(line 8v)" if ty.year >= 2024 else "(line 8z, 'Digital assets received as ordinary income')")
          + "; lines 9 and 10 show only this amount: add any other Schedule 1 income you have.", ""]
    for typ, usd in sorted(ty.income.items(), key=lambda kv: -kv[1]):
        if usd >= Decimal("0.005"):
            L.append(f"  - {typ}: ${usd:,.2f}")
    L += ["", "Answer **Yes** to the digital asset question on Form 1040.", "",
          "## Import files", "",
          "- `taxact_*.csv`: TaxAct 'Gain or loss on the sale of cryptocurrency' > CSV import (columns: Description, "
          "Date Acquired, Date Sold, Sales Proceeds, Cost or Other Basis, Reporting Category, Long/Short). Map the "
          "columns in TaxAct's import screen and check the totals against Schedule D above. "
          "`taxact_*_6col.csv` has the same lines without the Long/Short column (CoinLedger's TaxAct layout); "
          "use whichever TaxAct's importer accepts, not both.",
          "- `turbotax_*.csv`: the 7-column digital-asset gain/loss layout (Name, Received date, Cost or other basis, "
          "Date sold, Proceeds, Number of units, Form 8949 box) used by TurboTax for uploaded statements. TurboTax's "
          "accepted formats change; verify after import.",
          "- `form_8949_summary_totals_*.csv`: one row per Form 8949 box (proceeds, basis, gain, Schedule D line) "
          "for software where you enter totals instead of every sale (Cash App Taxes, IRS Free File Fillable "
          "Forms). Enter each box as one Form 8949 line described 'see attached statement', then mail the "
          "consolidated Form 8949 (its pages after the first are the line-by-line statement) with Form 8453 "
          "within 3 business days of the IRS accepting the e-filed return.", ""]
    if notes:
        L += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    L += ["## Files", ""] + [f"- `{f}`" for f in written]
    (out_dir / "README.md").write_text("\n".join(L) + "\n")
    (out_dir / "method_statement.txt").write_text(
        "Standing order for the identification of digital asset units\n\n"
        "Effective 2025-01-01, units of each digital asset sold, disposed of or transferred from any wallet or "
        "account are identified as the units with the highest cost basis held in that wallet or account "
        "(highest-in, first-out), and among equal basis the earliest acquired. Unused basis held on "
        "2025-01-01 was allocated to wallets and accounts under the Rev. Proc. 2024-28 safe harbor (specific "
        "unit allocation guided by the traced transfer history of each unit).\n\n"
        f"Written down: {datetime.now(UTC).strftime('%Y-%m-%d')}. For units outside a broker's custody the "
        "identification has to exist in your records no later than the time of each sale; if your 2025 records "
        "(e.g. the per-wallet HIFO setting of the software you used during 2025) predate the sales, keep those "
        "with this statement.\n")

