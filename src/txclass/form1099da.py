"""Form 1099-DA import and reconciliation.

Brokers deliver 1099-DA data as a CSV (transaction detail) or a PDF. CSV columns are recognised by common
header names (date sold, asset/token, units, gross proceeds, cost basis, date acquired, basis-reported flag);
PDFs are read as text and rows with a date, a quantity and a dollar amount are picked up best-effort (the
extracted text is kept so it can be reviewed when a layout is not understood).

Imported forms live in data/1099da/<year>/<id>.json (private, git-ignored, with the original file next to it).
Each row is matched to this project's Form 8949 lines of the same broker account: same asset, sold within a
day (brokers may date in local time), quantity within 0.5%. A 1099-DA also decides the Form 8949 box for
that account's sales: G/J when the form reports basis to the IRS, H/K otherwise.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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


def parse_pdf(body: bytes) -> tuple[list[Row], str]:
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages)
    rows = []
    for line in text.splitlines():
        m = ROW_RE.search(line)
        if m:
            d = parse_date(m.group("date"))
            p = _dec(m.group("proceeds"))
            if d and p is not None:
                rows.append(Row(d, m.group("asset"), m.group("qty").replace(",", ""), f"{p:f}"))
    return rows, text


def import_file(year: int, account: str, filename: str, body: bytes) -> Form:
    kind = "pdf" if body[:4] == b"%PDF" or filename.lower().endswith(".pdf") else "csv"
    if kind == "pdf":
        rows, text = parse_pdf(body)
    else:
        rows, text = parse_csv(body), ""
    f = Form(uuid.uuid4().hex[:12], year, account, Path(filename).name, kind, rows, text,
             datetime.now(UTC).strftime("%Y-%m-%d %H:%M"))
    d = DATA_DIR / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{f.id}.json").write_text(json.dumps(asdict(f), indent=1))
    (d / f"{f.id}{Path(filename).suffix or '.' + kind}").write_bytes(body)
    return f


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
class Match:
    row: Row
    line: Any | None  # taxforms.Line
    proceeds_diff: Decimal | None


def reconcile(form: Form, lines: list[Any]) -> dict[str, Any]:
    """Match the form's rows to this year's Form 8949 lines of the same account."""
    pool = [ln for ln in lines if ln.account == form.account]
    used: set[int] = set()
    matches = []
    for r in form.rows:
        fam = family(r.asset)
        q = _dec(r.qty)
        best = None
        for i, ln in enumerate(pool):
            if i in used or family(ln.asset) != fam:
                continue
            ld = datetime.strptime(ln.sold, "%m/%d/%Y")
            if abs((ld - datetime.strptime(r.date_sold, "%Y-%m-%d")).days) > 1:
                continue
            if q is not None and ln.qty and abs(ln.qty - q) > max(abs(q) * Decimal("0.005"), Decimal("1e-8")):
                continue
            diff = abs(ln.proceeds - (_dec(r.proceeds) or D0))
            if best is None or diff < best[1]:
                best = (i, diff)
        if best is not None:
            used.add(best[0])
            ln = pool[best[0]]
            matches.append(Match(r, ln, (_dec(r.proceeds) or D0) - ln.proceeds))
        else:
            matches.append(Match(r, None, None))
    reported = sum(((_dec(r.proceeds) or D0) for r in form.rows), D0)
    ours = sum((ln.proceeds for ln in pool), D0)
    return {"matches": matches, "matched": sum(1 for m in matches if m.line is not None),
            "unmatched_rows": [m.row for m in matches if m.line is None],
            "unreported_lines": [ln for i, ln in enumerate(pool) if i not in used],
            "reported_proceeds": reported, "our_proceeds": ours,
            "big_diffs": [m for m in matches if m.proceeds_diff is not None and abs(m.proceeds_diff) >= 1]}
