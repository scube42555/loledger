"""Fill official fillable PDF forms (e.g. IRS Form 1040-X) and assemble them into one mailing packet.

`fill` writes real AcroForm values, so the output stays editable. Field names are a widget's partial name, e.g.
"f1_04[0]" or "c1_3[1]", and pages are 0-based. IRS forms nest every widget under a page subform ("Page1[0]"),
so each checkbox widget is itself a terminal field. Its value (/V) must go on the widget, not on the parent
subform; otherwise viewers and flatteners show the box unchecked even though its appearance state (/AS) is on.
Unknown field names raise KeyError, so a mistyped mapping fails loudly instead of leaving a line blank.

`assemble` merges files in order (optionally selected pages). Any part that still has form fields is flattened
first with qpdf, which also builds appearance streams for the text values, so field names that repeat across
forms (f1_01[0] on both the 1040 and the 1040-X) can't collide in the packet.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, DictionaryObject, NameObject

Check = tuple[int, str] | tuple[int, str, str]  # (page, field) or (page, field, on-state such as "/2")
Part = Path | str | tuple[Path | str, Sequence[int]]  # whole file, or (file, 0-based page indexes)


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


def flatten(src: Path | str, out: Path | str) -> None:
    """Burn form values into the page content (qpdf), leaving no fillable fields."""
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        raise RuntimeError("qpdf is required to flatten filled forms (install the qpdf package)")
    subprocess.run([qpdf, "--generate-appearances", "--flatten-annotations=all", str(src), str(out)],
                   check=True, capture_output=True)


def assemble(parts: Sequence[Part], out: Path | str) -> int:
    """Merge `parts` in order into `out`, flattening any that still have form fields. Returns the page count."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    w = PdfWriter()
    with tempfile.TemporaryDirectory() as tmp:
        for i, part in enumerate(parts):
            path, pages = (Path(part[0]), list(part[1])) if isinstance(part, tuple) else (Path(part), None)
            if PdfReader(str(path)).get_fields():
                flat = Path(tmp) / f"{i}.pdf"
                flatten(path, flat)
                path = flat
            w.append(str(path), pages=pages, import_outline=False)
        with open(out, "wb") as fh:
            w.write(fh)
    return len(w.pages)
