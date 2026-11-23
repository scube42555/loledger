"""Filling and assembling fillable PDF forms, on a tiny form built like the IRS ones: every widget is a terminal
field nested under a "Page1[0]" subform."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                           TextStringObject)
from reportlab.pdfbase.pdfmetrics import stringWidth

from loledger.pdfforms import add_blank_fields, add_fields, assemble, fill, overlay


def _n(s: str) -> NameObject:
    return NameObject(s)


def _form(path: Path) -> None:
    w = PdfWriter()
    page = w.add_blank_page(300, 200)

    def ap(body: bytes) -> object:
        s = DecodedStreamObject()
        s.set_data(body)
        s.update({_n("/Type"): _n("/XObject"), _n("/Subtype"): _n("/Form"),
                  _n("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(10), FloatObject(10)])})
        return w._add_object(s)

    def rect(*v: float) -> ArrayObject:
        return ArrayObject([FloatObject(x) for x in v])

    parent = DictionaryObject({_n("/T"): TextStringObject("Page1[0]"), _n("/Kids"): ArrayObject()})
    pref = w._add_object(parent)
    widgets: list[DictionaryObject] = []
    for name, state in (("c1_1[0]", "/1"), ("c1_1[1]", "/2")):  # two checkboxes, one on-state each
        widgets.append(DictionaryObject({
            _n("/Type"): _n("/Annot"), _n("/Subtype"): _n("/Widget"), _n("/FT"): _n("/Btn"),
            _n("/T"): TextStringObject(name), _n("/Parent"): pref, _n("/F"): FloatObject(4),
            _n("/Rect"): rect(10, 10 + 30 * len(widgets), 20, 20 + 30 * len(widgets)),
            _n("/AS"): _n("/Off"), _n("/V"): _n("/Off"),
            _n("/AP"): DictionaryObject({_n("/N"): DictionaryObject({_n(state): ap(b"0 0 10 10 re f"),
                                                                     _n("/Off"): ap(b"")})})}))
    widgets.append(DictionaryObject({
        _n("/Type"): _n("/Annot"), _n("/Subtype"): _n("/Widget"), _n("/FT"): _n("/Tx"),
        _n("/T"): TextStringObject("f1_01[0]"), _n("/Parent"): pref, _n("/F"): FloatObject(4),
        _n("/Rect"): rect(50, 100, 250, 120), _n("/DA"): TextStringObject("/Helv 10 Tf 0 g")}))
    refs = [w._add_object(x) for x in widgets]
    parent[_n("/Kids")] = ArrayObject(refs)
    page[_n("/Annots")] = ArrayObject(refs)
    helv = DictionaryObject({_n("/Type"): _n("/Font"), _n("/Subtype"): _n("/Type1"),
                             _n("/BaseFont"): _n("/Helvetica"), _n("/Encoding"): _n("/WinAnsiEncoding")})
    w._root_object[_n("/AcroForm")] = DictionaryObject({
        _n("/Fields"): ArrayObject([pref]), _n("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
        _n("/DR"): DictionaryObject({_n("/Font"): DictionaryObject({_n("/Helv"): w._add_object(helv)})})})
    with open(path, "wb") as fh:
        w.write(fh)


def test_fill_sets_text_and_checkbox_value_on_the_widget_field(tmp_path: Path) -> None:
    src, out = tmp_path / "form.pdf", tmp_path / "filled.pdf"
    _form(src)
    fill(src, out, {0: {"f1_01[0]": "Married filing jointly"}}, [(0, "c1_1[1]")])
    fields = PdfReader(str(out)).get_fields() or {}
    assert fields["Page1[0].f1_01[0]"]["/V"] == "Married filing jointly"
    assert fields["Page1[0].c1_1[1]"]["/V"] == "/2"  # the regression: /V used to land on Page1[0]
    assert fields["Page1[0].c1_1[0]"]["/V"] == "/Off"
    assert "/V" not in fields["Page1[0]"]
    arr = PdfReader(str(out)).pages[0]["/Annots"].get_object()
    assert isinstance(arr, ArrayObject)
    annots = [a.get_object() for a in arr]
    assert [a.get("/AS") for a in annots[:2]] == ["/Off", "/2"]


def test_fill_rejects_unknown_fields_and_states(tmp_path: Path) -> None:
    src = tmp_path / "form.pdf"
    _form(src)
    with pytest.raises(KeyError, match="f1_99"):
        fill(src, tmp_path / "x.pdf", {0: {"f1_99[0]": "1"}})
    with pytest.raises(KeyError, match="c1_1"):
        fill(src, tmp_path / "x.pdf", {}, [(0, "c1_1[1]", "/3")])


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_assemble_flattens_filled_forms_in_order(tmp_path: Path) -> None:
    src, a, b = tmp_path / "form.pdf", tmp_path / "a.pdf", tmp_path / "b.pdf"
    _form(src)
    fill(src, a, {0: {"f1_01[0]": "FIRST FORM"}}, [(0, "c1_1[0]")])
    fill(src, b, {0: {"f1_01[0]": "SECOND FORM"}})  # same field names as `a`
    plain = PdfWriter()
    plain.add_blank_page(300, 200)
    plain.add_blank_page(300, 200)
    with open(tmp_path / "plain.pdf", "wb") as fh:
        plain.write(fh)
    out = tmp_path / "packet.pdf"
    assert assemble([b, (tmp_path / "plain.pdf", [1]), a], out) == 3
    r = PdfReader(str(out))
    assert "SECOND FORM" in r.pages[0].extract_text() and "FIRST FORM" in r.pages[2].extract_text()
    assert not r.pages[1].get("/Annots")
    # filled values are burnt in; the boxes left empty stay fillable, named per part so they can't collide
    fields = r.get_fields() or {}
    assert sorted(fields) == ["1-Page1[0]_c1_1[0]", "1-Page1[0]_c1_1[1]", "3-Page1[0]_c1_1[1]"]
    acro: Any = r.trailer["/Root"]
    acro = acro["/AcroForm"]
    assert acro["/NeedAppearances"] and "/Helv" in acro["/DR"]["/Font"]
    assert assemble([a], tmp_path / "flat.pdf", keep_empty=False) == 1
    assert not PdfReader(str(tmp_path / "flat.pdf")).get_fields()


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_assemble_keeps_an_empty_field_repeated_across_pages_as_one_field(tmp_path: Path) -> None:
    src, two = tmp_path / "form.pdf", tmp_path / "two.pdf"
    _form(src)
    r = PdfReader(str(src))
    w = PdfWriter()
    w.add_blank_page(300, 200)
    w.add_blank_page(300, 200)
    annots: Any = r.pages[0]["/Annots"]
    root: Any = r.trailer["/Root"]
    ssn = annots[2].get_object()  # the f1_01 text box, as a header field on both pages
    assert add_fields(w, {"ssn": [(0, ssn), (1, ssn)]}, {0: 0, 1: 1}, root["/AcroForm"]) == 1
    with open(two, "wb") as fh:
        w.write(fh)
    out = tmp_path / "packet.pdf"
    assemble([two], out)
    f = PdfReader(str(out)).get_fields() or {}
    assert len(f["1-ssn"]["/Kids"]) == 2  # one field, a widget on each page: typed once, shown twice


def _blank(path: Path, pages: int = 2) -> None:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    with open(path, "wb") as fh:
        w.write(fh)


def test_overlay_prints_marks_on_the_chosen_pages(tmp_path: Path) -> None:
    src, out = tmp_path / "blank.pdf", tmp_path / "marked.pdf"
    _blank(src)
    overlay(src, out, {1: [("l", 40, 700, "Taxpayer A."), ("r", 560, 680, "15,544"), ("x", 500, 660, ""),
                           ("l", 40, 640, ("small print", 6.0))]})
    r = PdfReader(str(out))
    assert len(r.pages) == 2 and not r.pages[0].extract_text().strip()
    text = r.pages[1].extract_text()
    for s in ("Taxpayer A.", "15,544", "X", "small print"):
        assert s in text
    seen: list[tuple[str, float, float]] = []
    r.pages[1].extract_text(visitor_text=lambda t, cm, tm, fd, fs: seen.append((t.strip(), tm[4], fs)) if t.strip() else None)
    start = next(x for t, x, _ in seen if t == "15,544")
    assert start + stringWidth("15,544", "Helvetica", 9) == pytest.approx(560)  # right-aligned: ends at x=560
    assert next(fs for t, _, fs in seen if t == "small print") == 6.0


def test_overlay_rejects_bad_pages_and_kinds(tmp_path: Path) -> None:
    src = tmp_path / "blank.pdf"
    _blank(src)
    with pytest.raises(IndexError):
        overlay(src, tmp_path / "x.pdf", {2: [("l", 0, 0, "a")]})
    with pytest.raises(ValueError):
        overlay(src, tmp_path / "x.pdf", {0: [("c", 0, 0, "a")]})


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_assemble_wraps_a_long_multiline_value_inside_its_box(tmp_path: Path) -> None:
    src, filled, out = tmp_path / "form.pdf", tmp_path / "filled.pdf", tmp_path / "packet.pdf"
    _form(src)
    w = PdfWriter(clone_from=PdfReader(str(src)))
    annots: Any = w.pages[0]["/Annots"]
    box = annots[2].get_object()
    box[NameObject("/Ff")] = FloatObject(4096)  # multi-line, like the 1040-X Part II explanation
    box[NameObject("/Rect")] = ArrayObject([FloatObject(x) for x in (20, 20, 280, 120)])
    with open(src, "wb") as fh:
        w.write(fh)
    text = " ".join(f"word{i}" for i in range(80)) + " THE END"
    fill(src, filled, {0: {"f1_01[0]": text}})
    assemble([filled], out)
    page = PdfReader(str(out)).pages[0]
    got = page.extract_text()
    assert "word0" in got and "THE END" in got  # qpdf alone keeps only what fits on the first line
    xs: list[float] = []
    page.extract_text(visitor_text=lambda t, cm, tm, fd, fs: xs.append(tm[4]) if t.strip() else None)
    assert xs and min(xs) >= 20 and max(xs) < 280


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_blank_fields_on_a_plain_form_and_shared_ones_merge_across_packet_parts(tmp_path: Path) -> None:
    a, b, out = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "packet.pdf"
    _blank(a, 2)
    _blank(b, 1)
    add_blank_fields(a, a, {0: [("shared_ssn", (40, 700, 200, 714), "text"), ("phone", (40, 680, 200, 694), "text"),
                                ("ok", (300, 680, 310, 690), "check")],
                            1: [("shared_ssn", (400, 750, 500, 764), "text")]})
    add_blank_fields(b, b, {0: [("shared_ssn", (40, 700, 200, 714), "text")]})
    f = PdfReader(str(a)).get_fields() or {}
    assert len(f["shared_ssn"]["/Kids"]) == 2 and f["ok"]["/FT"] == "/Btn" and f["phone"]["/FT"] == "/Tx"
    with pytest.raises(ValueError):
        add_blank_fields(b, tmp_path / "x.pdf", {0: [("x", (0, 0, 1, 1), "radio")]})
    assert assemble([a, b], out) == 3
    f = PdfReader(str(out)).get_fields() or {}
    assert len(f["shared_ssn"]["/Kids"]) == 3  # one SSN for the whole packet
    assert "1-phone" in f and "1-ok" in f
    r = PdfReader(str(out))
    assert [len(p.get("/Annots") or []) for p in r.pages] == [3, 1, 1]  # no leftover copies of the boxes
    # rename links another part's empty field into the shared one (an attached 1040-X copy's SSN box)
    src, x = tmp_path / "form.pdf", tmp_path / "x.pdf"
    _form(src)
    fill(src, x, {0: {"f1_01[0]": ""}})
    assemble([a, x], out, rename={1: {"f1_01[0]": "shared_ssn"}})
    f = PdfReader(str(out)).get_fields() or {}
    assert len(f["shared_ssn"]["/Kids"]) == 3 and not any(k.endswith("f1_01[0]") for k in f)
