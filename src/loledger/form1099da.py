"""Form 1099-DA import and reconciliation.

Brokers deliver 1099-DA data as a CSV (transaction detail) or a PDF. CSV columns are recognised by common
header names (date sold, asset/token, units, gross proceeds, cost basis, date acquired, basis-reported flag);
PDFs are read as text and rows with a date, a quantity and a dollar amount are picked up best-effort (the
extracted text is kept so it can be reviewed when a layout is not understood).

Imported forms live in data/1099da/<year>/<id>.json (private, git-ignored, with the original file next to it).
The form is compared with this project's Form 8949 lines of the same broker account per asset per day (brokers
list each lot as a row; our lines combine a disposal's lots), allowing a day's shift for local-time dating and
0.5% on units. A 1099-DA also decides the Form 8949 box for
that account's sales: G/J when the form reports basis to the IRS, H/K otherwise.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .reconcile import family

DATA_DIR = Path("data/1099da")
D0 = Decimal(0)

HEADERS = {  # field -> header fragments (lowercase, punctuation stripped); first match wins
    "date_sold": ["date sold", "sale date", "date of sale", "disposition date", "date disposed", "sold date",
                  "date sold or disposed", "1c", "closed date", "date"],
    "asset": ["digital asset name", "asset name", "digital asset", "asset", "token", "currency", "symbol",
              "coin", "1a", "description"],
    "qty": ["number of units", "units", "quantity", "qty", "1b", "amount sold", "amount"],
    "proceeds": ["gross proceeds", "proceeds", "1f", "sales price", "sale proceeds"],
    "basis": ["cost or other basis", "cost basis", "basis", "1g", "cost"],
    "acquired": ["date acquired", "acquisition date", "acquired", "1e", "purchase date"],
    "basis_reported": ["basis reported", "covered", "noncovered", "box 12", "reported to irs"],
    "term": ["term", "short long", "gain type", "holding period"],
}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d %b %Y",
                "%b %d, %Y", "%Y/%m/%d", "%m-%d-%Y")


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", h.lower()).strip()


def _dec(s: str) -> Decimal | None:
    s = (s or "").strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    s = re.sub(r"\s*(UTC|Z|[+-]\d\d:?\d\d)$", "", s)
    for cand in (s, s[:19], s[:10], s.split()[0] if s else ""):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


@dataclass
class Row:
    date_sold: str  # YYYY-MM-DD
    asset: str
    qty: str
    proceeds: str
    basis: str = ""
    acquired: str = ""
    basis_reported: bool = False
    term: str = ""


@dataclass
class Form:
    id: str
    year: int
    account: str  # this project's account name (e.g. "Gemini")
    filename: str
    kind: str  # csv | pdf
    rows: list[Row] = field(default_factory=list)
    text: str = ""  # PDF text (for review)
    imported: str = ""
    form_total: str = ""  # grand total proceeds printed on the PDF's summary, to check the parse

    def total_check(self) -> tuple[Decimal, Decimal] | None:
        """(form total, sum of parsed rows) when the form prints a total."""
        if not self.form_total:
            return None
        return Decimal(self.form_total), sum(((_dec(r.proceeds) or D0) for r in self.rows), D0)


def _map_headers(header: list[str]) -> dict[str, int]:
    norm = [_norm(h) for h in header]
    out: dict[str, int] = {}
    for fld, frags in HEADERS.items():
        for frag in frags:
            hit = next((i for i, h in enumerate(norm) if i not in out.values() and (h == frag or frag in h.split()
                                                                                    or (len(frag) > 3 and frag in h))), None)
            if hit is not None:
                out[fld] = hit
                break
    return out


def parse_csv(body: bytes) -> list[Row]:
    text = body.decode("utf-8-sig", "replace")
    lines = list(csv.reader(io.StringIO(text)))
    # the header is the first line that names a sale date and proceeds
    for hi, h in enumerate(lines[:30]):
        m = _map_headers(h)
        if "date_sold" in m and "proceeds" in m and "asset" in m:
            break
    else:
        raise ValueError("no header with a sale date, an asset and proceeds was found in this CSV")
    rows = []
    for r in lines[hi + 1:]:
        if len(r) <= max(m.values()):
            continue
        d = parse_date(r[m["date_sold"]])
        p = _dec(r[m["proceeds"]])
        if not d or p is None:
            continue
        q = _dec(r[m["qty"]]) if "qty" in m else None
        b = _dec(r[m["basis"]]) if "basis" in m else None
        asset = r[m["asset"]].strip()
        if q is None:  # descriptions like "0.5 BTC"
            mm = re.match(r"\s*([\d.,]+)\s+([A-Za-z0-9.]+)", asset)
            if mm:
                q, asset = _dec(mm.group(1)), mm.group(2)
        br = r[m["basis_reported"]].strip().lower() if "basis_reported" in m else ""
        paren = re.search(r"\(([A-Za-z0-9.]{2,12})\)", asset)  # "Bitcoin Cash (BCH)"
        asset = paren.group(1) if paren else (asset.split()[-1] if asset else "")
        rows.append(Row(d, asset, "" if q is None else f"{q:f}", f"{p:f}",
                        "" if b is None else f"{b:f}",
                        (parse_date(r[m["acquired"]]) or r[m["acquired"]].strip()) if "acquired" in m else "",
                        br in ("y", "yes", "true", "x", "covered", "1"), r[m["term"]].strip() if "term" in m else ""))
    return rows


ROW_RE = re.compile(r"(?P<asset>[A-Z][A-Z0-9.]{1,9})\s+(?P<qty>\d[\d,]*\.\d+)\s+(?P<date>\d{2}/\d{2}/\d{4}).*?"
                    r"\$?(?P<proceeds>\(?[\d,]+\.\d{2}\)?)")


ASSET_RE = re.compile(r"^(?P<name>[A-Z][A-Za-z0-9 .&'-]*?)\s*\((?P<sym>[A-Za-z0-9.]{1,12})\)\s+-\s+\S+")
NUM_RE = re.compile(r"^\d[\d,]*(\.\d+)?$")
MONEY_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _section(line: str) -> tuple[str, bool] | None:
    """(term, basis_reported) from a broker's section heading ("The section below summarizes ...")."""
    low = line.lower()
    if not low.startswith("the section below"):
        return None
    term = ("short" if "short-term" in low else "long" if "long-term" in low else "")
    return term, "noncovered" not in low


def _parse_stacked(lines: list[str]) -> list[Row]:
    """Layouts that print one field per line (Gemini and others built on the same 1099-DA generator):
    asset heading "BITCOIN (BTC) - CUSIP", then per sale: units, date acquired (date | Various | -),
    date sold, proceeds, then "basis customer-info" -- on the proceeds line, or after an "X" line when
    the proceeds were cash only."""
    toks = [t for t in (ln.strip() for ln in lines) if t]
    rows: list[Row] = []
    asset, term, reported = "", "", False
    i = 0
    while i < len(toks):
        t = toks[i]
        sec = _section(t)
        if sec:
            term, reported = sec
        elif m := ASSET_RE.match(t):
            asset = m.group("sym").upper()
        elif (asset and NUM_RE.match(t) and i + 3 < len(toks)
              and (DATE_RE.match(toks[i + 1]) or toks[i + 1] in ("Various", "VARIOUS", "-"))
              and DATE_RE.match(toks[i + 2]) and MONEY_RE.match(toks[i + 3].split()[0])):
            words = toks[i + 3].split()
            if len(words) > 1:  # not cash-only: "proceeds basis customer-info" share a line
                j, bt = i + 3, words[1]
            else:  # "proceeds" / "X" / "basis customer-info"
                j = i + 4 + (i + 4 < len(toks) and toks[i + 4] == "X")
                bt = toks[j].split()[0] if j < len(toks) else "-"
            basis = _dec(bt) if MONEY_RE.match(bt) else None
            acq = toks[i + 1]
            p = _dec(words[0])
            d = parse_date(toks[i + 2])
            if d and p is not None:
                rows.append(Row(d, asset, t.replace(",", ""), f"{p:f}", "" if basis is None else f"{basis:f}",
                                parse_date(acq) or ("VARIOUS" if acq.lower() == "various" else ""),
                                reported, term))
                i = j + 1
                continue
        i += 1
    return rows


def parse_pdf(body: bytes) -> tuple[list[Row], str]:
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages)
    rows = _parse_stacked(text.splitlines())
    if rows:
        return rows, text
    for line in text.splitlines():  # one sale per line
        m = ROW_RE.search(line)
        if m:
            d = parse_date(m.group("date"))
            p = _dec(m.group("proceeds"))
            if d and p is not None:
                rows.append(Row(d, m.group("asset"), m.group("qty").replace(",", ""), f"{p:f}"))
    return rows, text


def summary_totals(text: str) -> Decimal | None:
    """The form's grand total proceeds (the line of bare totals under the summary table), to check the parse."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and all(MONEY_RE.match(x) or x == "-" for x in parts) and MONEY_RE.match(parts[0]):
            return _dec(parts[0])
    return None


def import_file(year: int, account: str, filename: str, body: bytes) -> Form:
    kind = "pdf" if body[:4] == b"%PDF" or filename.lower().endswith(".pdf") else "csv"
    if kind == "pdf":
        rows, text = parse_pdf(body)
    else:
        rows, text = parse_csv(body), ""
    tot = summary_totals(text) if text else None
    f = Form(uuid.uuid4().hex[:12], year, account, Path(filename).name, kind, rows, text,
             datetime.now(UTC).strftime("%Y-%m-%d %H:%M"), "" if tot is None else f"{tot:f}")
    d = DATA_DIR / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{f.id}.json").write_text(json.dumps(asdict(f), indent=1))
    (d / f"{f.id}{Path(filename).suffix or '.' + kind}").write_bytes(body)
    return f


def reparse_all() -> int:
    """Re-read every stored PDF with the current parser (after a parser improvement). Returns forms updated."""
    n = 0
    for f in load_forms():
        if f.kind != "pdf":
            continue
        src = next((p for p in (DATA_DIR / str(f.year)).glob(f"{f.id}.*") if p.suffix != ".json"), None)
        if src is None:
            continue
        f.rows, f.text = parse_pdf(src.read_bytes())
        tot = summary_totals(f.text)
        f.form_total = "" if tot is None else f"{tot:f}"
        (DATA_DIR / str(f.year) / f"{f.id}.json").write_text(json.dumps(asdict(f), indent=1))
        n += 1
    return n


def load_forms(year: int | None = None) -> list[Form]:
    out = []
    dirs = [DATA_DIR / str(year)] if year else sorted(p for p in DATA_DIR.glob("*") if p.is_dir())
    for d in dirs:
        for p in sorted(d.glob("*.json")):
            raw = json.loads(p.read_text())
            raw["rows"] = [Row(**r) for r in raw.get("rows", [])]
            out.append(Form(**raw))
    return out


def delete_form(year: int, form_id: str) -> None:
    for p in (DATA_DIR / str(year)).glob(f"{form_id}*"):
        p.unlink()


def broker_accounts(year: int) -> dict[str, bool]:
    """Accounts with an imported 1099-DA for `year` -> whether any row reports basis to the IRS."""
    out: dict[str, bool] = {}
    for f in load_forms(year):
        out[f.account] = out.get(f.account, False) or any(r.basis_reported for r in f.rows)
    return out


# ---------------------------------------------------------------------------------------- reconcile


@dataclass
class DayGroup:
    """One asset's sales on one day, as the 1099-DA lists them (often one row per lot) and as our Form 8949 has
    them (one line per disposal)."""
    day: str  # YYYY-MM-DD (the 1099-DA's date when both sides exist)
    asset: str
    rows: list[Row] = field(default_factory=list)
    lines: list[Any] = field(default_factory=list)  # taxforms.Line

    @property
    def da_qty(self) -> Decimal:
        return sum(((_dec(r.qty) or D0) for r in self.rows), D0)

    @property
    def da_proceeds(self) -> Decimal:
        return sum(((_dec(r.proceeds) or D0) for r in self.rows), D0)

    @property
    def da_basis(self) -> Decimal | None:
        bs = [_dec(r.basis) for r in self.rows if r.basis]
        return sum((b for b in bs if b is not None), D0) if bs else None

    @property
    def our_qty(self) -> Decimal:
        return sum((ln.qty for ln in self.lines), D0)

    @property
    def our_proceeds(self) -> Decimal:
        return sum((ln.records_proceeds for ln in self.lines), D0)

    @property
    def our_basis(self) -> Decimal:
        return sum((ln.basis for ln in self.lines), D0)

    @property
    def proceeds_diff(self) -> Decimal | None:
        return (self.da_proceeds - self.our_proceeds) if self.rows and self.lines else None

    @property
    def qty_ok(self) -> bool:
        return _qty_close(self.da_qty, self.our_qty)

    @property
    def status(self) -> str:
        if not self.lines:
            return "only_form"
        if not self.rows:
            return "only_ours"
        if not self.qty_ok:
            return "units_differ"
        return "match" if abs(self.proceeds_diff or D0) < 1 else "proceeds_differ"


def _qty_close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(a) * Decimal("0.005"), Decimal("1e-8"))


def _day(sold: str) -> str:
    return datetime.strptime(sold, "%m/%d/%Y").strftime("%Y-%m-%d")


def reconcile(form: Form, lines: list[Any]) -> dict[str, Any]:
    """Compare the form with this year's Form 8949 lines of the same account, per asset per day: brokers list
    each lot sold as its own row, while our lines combine the lots of one disposal. A day on one side only is
    paired with the same asset's day before/after on the other side when the units agree (brokers may date
    in local time)."""
    pool = [ln for ln in lines if ln.account == form.account]
    groups: dict[tuple[str, str], DayGroup] = {}
    for r in form.rows:
        groups.setdefault((family(r.asset), r.date_sold), DayGroup(r.date_sold, r.asset)).rows.append(r)
    for ln in pool:
        k = (family(ln.asset), _day(ln.sold))
        groups.setdefault(k, DayGroup(k[1], ln.asset)).lines.append(ln)
    for (fam, day), g in list(groups.items()):
        if g.lines or (fam, day) not in groups:
            continue
        for off in (-1, 1):
            od = (date.fromisoformat(day) + timedelta(days=off)).isoformat()
            o = groups.get((fam, od))
            if o and not o.rows and _qty_close(g.da_qty, o.our_qty):
                g.lines = o.lines
                del groups[(fam, od)]
                break
    gs = sorted(groups.values(), key=lambda g: (g.day, g.asset))
    both = [g for g in gs if g.rows and g.lines and g.qty_ok]
    return {"groups": gs, "matched": sum(len(g.rows) for g in both),
            "days_matched": sum(1 for g in gs if g.status == "match"),
            "unmatched_rows": [r for g in gs if g.status in ("only_form", "units_differ") for r in g.rows],
            "unreported_lines": [ln for g in gs if g.status == "only_ours" for ln in g.lines],
            "reported_proceeds": sum((g.da_proceeds for g in gs), D0),
            "our_proceeds": sum((ln.records_proceeds for ln in pool), D0),
            "big_diffs": [g for g in gs if g.status in ("units_differ", "proceeds_differ")]}


def apply_reported_proceeds(year: int, lines: list[Any]) -> tuple[int, Decimal]:
    """Give the Form 8949 lines covered by the year's 1099-DAs the proceeds the broker reported (so the IRS's
    matching agrees). Only asset-days whose units agree are changed; a day's reported total is spread over its
    lines in proportion to our proceeds (units when those are zero), the last line taking the rounding.
    Basis is untouched. Returns (lines changed, net change in proceeds)."""
    n, delta = 0, D0
    for form in load_forms(year):
        for g in reconcile(form, lines)["groups"]:
            if not (g.rows and g.lines and g.qty_ok) or g.proceeds_diff == 0:
                continue
            target = g.da_proceeds
            ours = g.our_proceeds
            weights = [ln.records_proceeds for ln in g.lines] if ours else [ln.qty for ln in g.lines]
            wsum = sum(weights, D0) or Decimal(len(g.lines))
            left = target
            for i, ln in enumerate(g.lines):
                new = left if i == len(g.lines) - 1 else (target * (weights[i] or 1) / wsum).quantize(Decimal("0.01"))
                left -= new
                if new != ln.proceeds:
                    if ln.own_proceeds is None:
                        ln.own_proceeds = ln.proceeds
                    delta += new - ln.proceeds
                    ln.proceeds = new
                    n += 1
    return n, delta
