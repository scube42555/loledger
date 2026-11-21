"""Filling and assembling fillable PDF forms, on a tiny form built like the IRS ones: every widget is a terminal
field nested under a "Page1[0]" subform."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                           TextStringObject)

from txclass.pdfforms import assemble, fill


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
    widgets = []
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
    annots = [a.get_object() for a in PdfReader(str(out)).pages[0]["/Annots"]]
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
    assert not r.get_fields()
    assert "SECOND FORM" in r.pages[0].extract_text() and "FIRST FORM" in r.pages[2].extract_text()
    assert not r.pages[1].get("/Annots")
