"""Fill official fillable PDF forms (e.g. IRS Form 1040-X) and assemble them into one mailing packet.

`fill` writes real AcroForm values, so the output stays editable. Field names are a widget's partial name, e.g.
"f1_04[0]" or "c1_3[1]", and pages are 0-based. IRS forms nest every widget under a page subform ("Page1[0]"),
so each checkbox widget is itself a terminal field. Its value (/V) must go on the widget, not on the parent
subform; otherwise viewers and flatteners show the box unchecked even though its appearance state (/AS) is on.
Unknown field names raise KeyError, so a mistyped mapping fails loudly instead of leaving a line blank.

`overlay` prints values at fixed positions onto forms that have no fillable fields (South Carolina's SCDOR
forms, for example), so they read like a typed-in form.

`assemble` merges files in order (optionally selected pages). Any part that still has form fields is flattened
first with qpdf, which also builds appearance streams for the text values (multi-line boxes, such as the 1040-X
Part II explanation, are drawn word-wrapped by us instead: qpdf puts them on one line), so field names that repeat across
forms (f1_01[0] on both the 1040 and the 1040-X) can't collide in the packet. Fields left EMPTY (SSN, phone,
occupation, dependents, ...) stay fillable: they are put back on top of the flattened page under a name unique to
the part, so the packet can be opened in any PDF viewer, completed and saved. A field that appears on several
pages of one part (the name/SSN header of a multi-page Form 8949) stays one field: typing it once fills every page.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, NameObject, TextStringObject

Check = tuple[int, str] | tuple[int, str, str]  # (page, field) or (page, field, on-state such as "/2")
Part = Path | str | tuple[Path | str, Sequence[int]]  # whole file, or (file, 0-based page indexes)
Mark = tuple[str, float, float, str | tuple[str, float]]  # (kind "l"/"r"/"x", x, y, text or (text, font size))


def _name(annot: DictionaryObject) -> Any:
    """Partial name of the field a widget belongs to: its own /T, else its parent's."""
    if "/T" in annot:
        return annot["/T"]
    parent = annot.get("/Parent")
    return parent.get_object().get("/T") if parent is not None else None


def _on_states(annot: DictionaryObject) -> list[str]:
    ap = annot.get("/AP")
    normal = ap.get_object().get("/N") if ap is not None else None
    return [str(k) for k in normal.get_object().keys() if k != "/Off"] if normal is not None else []


def _set_check(page: Any, name: str, state: str | None) -> bool:
    hit = False
    for ref in page.get("/Annots") or []:
        annot = ref.get_object()
        states = _on_states(annot)
        if _name(annot) != name or not states or (state is not None and state not in states):
            continue
        on = state or states[0]
        annot[NameObject("/AS")] = NameObject(on)
        field = annot if "/T" in annot else annot["/Parent"].get_object()
        field[NameObject("/V")] = NameObject(on)
        if field is not annot:  # radio-style group: siblings without this state turn off
            for kid in field.get("/Kids") or []:
                k = kid.get_object()
                if k is not annot:
                    k[NameObject("/AS")] = NameObject(on if on in _on_states(k) else "/Off")
        hit = True
    return hit


def fill(src: Path | str, out: Path | str, values: Mapping[int, Mapping[str, str]],
         checks: Iterable[Check] = ()) -> None:
    """Copy form `src` to `out` with text `values` ({page: {field: text}}) and `checks` turned on."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    w = PdfWriter(clone_from=PdfReader(str(src)))
    for pi, vals in values.items():
        page = w.pages[pi]
        known = {_name(a.get_object()) for a in page.get("/Annots") or []}
        missing = sorted(set(vals) - known)
        if missing:
            raise KeyError(f"page {pi}: no field(s) {', '.join(missing)} in {src}")
        w.update_page_form_field_values(page, dict(vals), auto_regenerate=False)
    for check in checks:
        pi, name = check[0], check[1]
        state = check[2] if len(check) == 3 else None
        if not _set_check(w.pages[pi], name, state):
            raise KeyError(f"page {pi}: no checkbox {name}{' ' + state if state else ''} in {src}")
    acro = w._root_object["/AcroForm"].get_object()
    assert isinstance(acro, DictionaryObject)
    acro[NameObject("/NeedAppearances")] = BooleanObject(True)
    with open(out, "wb") as fh:
        w.write(fh)


def overlay(src: Path | str, out: Path | str, marks: Mapping[int, Sequence[Mark]]) -> None:
    """Print `marks` ({page: [(kind, x, y, text)]}) onto a form that has no fillable fields (e.g. SCDOR forms).

    kind "l" draws text left-aligned at x, "r" right-aligned ending at x (amounts), "x" a bold X (checkbox). Text
    is 9 pt Helvetica; pass (text, size) for another size. Coordinates are PDF points from the bottom-left corner.
    """
    from io import BytesIO

    from reportlab.pdfgen import canvas

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    w = PdfWriter(clone_from=PdfReader(str(src)))
    for pi, items in marks.items():
        if not 0 <= pi < len(w.pages):
            raise IndexError(f"page {pi} not in {src} ({len(w.pages)} pages)")
        page = w.pages[pi]
        box = page.mediabox
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
        for kind, x, y, text in items:
            s, size = text if isinstance(text, tuple) else (text, 9.0)
            if kind == "x":
                c.setFont("Helvetica-Bold", 10)
                c.drawString(x, y, "X")
            elif kind in ("l", "r"):
                c.setFont("Helvetica", size)
                (c.drawRightString if kind == "r" else c.drawString)(x, y, s)
            else:
                raise ValueError(f"mark kind must be 'l', 'r' or 'x', not {kind!r}")
        c.save()
        page.merge_page(PdfReader(BytesIO(buf.getvalue())).pages[0])
    with open(out, "wb") as fh:
        w.write(fh)


INHERITED = ("/FT", "/Ff", "/DA", "/Q", "/MaxLen", "/TU")
FIELD_KEYS = ("/T", "/FT", "/Ff", "/V", "/DV", "/Kids", "/Parent", "/P", "/AA", "/TU", "/MaxLen", "/Q", "/DA")
Widget = tuple[int, DictionaryObject]  # (page index, widget annotation)


def _field_of(annot: DictionaryObject) -> tuple[str, DictionaryObject, dict[str, Any]] | None:
    """(full field name, terminal field, inherited attributes) of a widget, or None if it isn't a form field."""
    field: Any = annot if "/T" in annot else annot.get("/Parent", DictionaryObject()).get_object()
    if "/T" not in field:
        return None
    names: list[str] = []
    attrs: dict[str, Any] = {}
    node: Any = field
    while node is not None:
        if "/T" in node:
            names.append(str(node["/T"]))
        for k in INHERITED:
            if k in node and k not in attrs:
                attrs[k] = node[k]
        par = node.get("/Parent")
        node = par.get_object() if par is not None else None
    for k in INHERITED:  # the widget's own values win (merged field + widget)
        if k in annot:
            attrs[k] = annot[k]
    return ".".join(reversed(names)), field, attrs


def empty_fields(reader: PdfReader) -> dict[str, list[Widget]]:
    """Text fields and checkboxes with no value, by full field name -> their widgets (page index, annotation).
    Push buttons and read-only fields are left out; a checkbox group counts as filled if any box is on."""
    groups: dict[str, list[Widget]] = {}
    filled: set[str] = set()
    for pi, page in enumerate(reader.pages):
        for ref in page.get("/Annots") or []:
            a = ref.get_object()
            if a.get("/Subtype") != "/Widget":
                continue
            f = _field_of(a)
            if f is None:
                continue
            name, field, attrs = f
            ft, ff = attrs.get("/FT"), int(attrs.get("/Ff", 0))
            if ft not in ("/Tx", "/Btn") or ff & 1 or (ft == "/Btn" and ff & (1 << 16)):
                continue
            v = field.get("/V")
            if (v is not None and str(v) not in ("", "/Off")) or str(a.get("/AS", "/Off")) != "/Off":
                filled.add(name)
            groups.setdefault(name, []).append((pi, a))
    return {n: ws for n, ws in groups.items() if n not in filled}


def add_fields(w: PdfWriter, groups: Mapping[str, Sequence[Widget]], page_map: Mapping[int, int],
               src_acroform: DictionaryObject | None = None) -> int:
    """Copy the widgets of `groups` ({new field name: [(source page, widget)]}) onto writer pages
    (page_map: source page -> writer page index) as fresh empty fields, one field per name with a widget on each
    page, so typing it once fills every copy. Merges the source form's default resources (fonts) and sets
    NeedAppearances. Returns the number of fields added."""
    acro: Any = w._root_object.get("/AcroForm")
    if acro is None:
        acro = DictionaryObject({NameObject("/Fields"): ArrayObject()})
        w._root_object[NameObject("/AcroForm")] = w._add_object(acro)
    acro = acro.get_object()
    if src_acroform is not None:
        dr: Any = acro.setdefault(NameObject("/DR"), DictionaryObject()).get_object()
        src_dr: Any = (src_acroform.get("/DR") or DictionaryObject()).get_object()
        for kind, res in src_dr.items():
            dst: Any = dr.setdefault(NameObject(kind), DictionaryObject()).get_object()
            for k, v in res.get_object().items():
                if k not in dst:
                    dst[NameObject(k)] = v.clone(w)
        if "/DA" in src_acroform and "/DA" not in acro:
            acro[NameObject("/DA")] = TextStringObject(str(src_acroform["/DA"]))
    acro[NameObject("/NeedAppearances")] = BooleanObject(True)
    fields: Any = acro.setdefault(NameObject("/Fields"), ArrayObject())
    n = 0
    for name, widgets in groups.items():
        kids = [(page_map[pi], a) for pi, a in widgets if pi in page_map]
        if not kids:
            continue
        attrs = _field_of(kids[0][1])
        parent: Any = DictionaryObject({NameObject("/T"): TextStringObject(name), NameObject("/Kids"): ArrayObject()})
        for k, v in (attrs[2] if attrs else {}).items():
            parent[NameObject(k)] = v.clone(w) if hasattr(v, "clone") else v
        pref = w._add_object(parent)
        for wi, a in kids:
            page: Any = w.pages[wi]
            # a new widget per page (cloning `a` again would return the cached copy); its appearance streams
            # and other values are shared clones
            kid: Any = DictionaryObject({NameObject(k): v.clone(w) for k, v in a.items() if k not in FIELD_KEYS})
            kid[NameObject("/Parent")] = pref
            kid[NameObject("/P")] = page.indirect_reference
            if "/AS" in kid:
                kid[NameObject("/AS")] = NameObject("/Off")
            ref = w._add_object(kid)
            if "/Annots" not in page:
                page[NameObject("/Annots")] = ArrayObject()
            page["/Annots"].get_object().append(ref)
            parent["/Kids"].append(ref)
        fields.append(pref)
        n += 1
    return n


Wrapped = tuple[list[float], str, float]  # (rect, text, font size; 0 = auto)


def _take_multiline(src: Path, out: Path) -> dict[int, list[Wrapped]]:
    """Copy `src` to `out` with every filled multi-line text field emptied, and return those values by page.
    qpdf's flattening draws a field's value on one line, so a long explanation (1040-X Part II) would be cut
    off; `_draw_wrapped` puts it back, word-wrapped inside the box."""
    w = PdfWriter(clone_from=PdfReader(str(src)))
    found: dict[int, list[Wrapped]] = {}
    for pi, page in enumerate(w.pages):
        for ref in page.get("/Annots") or []:
            a: Any = ref.get_object()
            f = _field_of(a) if a.get("/Subtype") == "/Widget" else None
            if f is None:
                continue
            _, field, attrs = f
            v = field.get("/V")
            if attrs.get("/FT") != "/Tx" or not int(attrs.get("/Ff", 0)) & 4096 or not v or not str(v).strip():
                continue
            m = re.search(r"([\d.]+)\s+Tf", str(attrs.get("/DA", "")))
            found.setdefault(pi, []).append(([float(x) for x in a["/Rect"]], str(v), float(m.group(1)) if m else 0.0))
            field[NameObject("/V")] = TextStringObject("")
            if "/AP" in a:
                del a["/AP"]
    with open(out, "wb") as fh:
        w.write(fh)
    return found


def _draw_wrapped(src: Path, out: Path, items: Mapping[int, Sequence[Wrapped]]) -> None:
    """Print each text word-wrapped inside its box (Helvetica, shrunk until it fits, 5 pt at the smallest)."""
    from io import BytesIO

    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    w = PdfWriter(clone_from=PdfReader(str(src)))
    for pi, boxes in items.items():
        box = w.pages[pi].mediabox
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)))
        for (x0, y0, x1, y1), text, size in boxes:
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            size = size or 10.0
            while True:
                lines = [ln for para in text.splitlines() or [text]
                         for ln in (simpleSplit(para, "Helvetica", size, x1 - x0 - 4) or [""])]
                if len(lines) * size * 1.2 <= y1 - y0 - 2 or size <= 5:
                    break
                size -= 0.25
            c.setFont("Helvetica", size)
            y = y1 - 1 - size
            for ln in lines:
                c.drawString(x0 + 2, y, ln)
                y -= size * 1.2
        c.save()
        w.pages[pi].merge_page(PdfReader(BytesIO(buf.getvalue())).pages[0])
    with open(out, "wb") as fh:
        w.write(fh)


def flatten(src: Path | str, out: Path | str) -> None:
    """Burn form values into the page content (qpdf), leaving no fillable fields."""
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        raise RuntimeError("qpdf is required to flatten filled forms (install the qpdf package)")
    subprocess.run([qpdf, "--generate-appearances", "--flatten-annotations=all", str(src), str(out)],
                   check=True, capture_output=True)


def assemble(parts: Sequence[Part], out: Path | str, keep_empty: bool = True) -> int:
    """Merge `parts` in order into `out`, flattening any that still have form fields; with keep_empty, fields left
    empty stay fillable, named "<part number>-<field name>". Returns the page count."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    w = PdfWriter()
    with tempfile.TemporaryDirectory() as tmp:
        for i, part in enumerate(parts):
            path, pages = (Path(part[0]), list(part[1])) if isinstance(part, tuple) else (Path(part), None)
            src = PdfReader(str(path))
            empty: dict[str, list[Widget]] = {}
            if src.get_fields():
                if keep_empty:
                    empty = empty_fields(src)
                pre, flat = Path(tmp) / f"{i}-pre.pdf", Path(tmp) / f"{i}.pdf"
                wrapped = _take_multiline(path, pre)
                flatten(pre, flat)
                if wrapped:
                    _draw_wrapped(flat, flat, wrapped)
                path = flat
            start = len(w.pages)
            w.append(str(path), pages=pages, import_outline=False)
            if empty:
                order = pages if pages is not None else list(range(len(src.pages)))
                root: Any = src.trailer["/Root"]
                acro: Any = root.get_object().get("/AcroForm")
                add_fields(w, {f"{i + 1}-{name.replace('.', '_')}": ws for name, ws in empty.items()},
                           {p: start + j for j, p in enumerate(order)}, acro.get_object() if acro else None)
        with open(out, "wb") as fh:
            w.write(fh)
    return len(w.pages)
