"""Local web app: portfolio, per-year tax report with exports, and Form 1099-DA import.

Run with `classify serve` (binds to 127.0.0.1 only). Everything is computed locally from classified.csv
and manual.yaml; the only outbound calls are DefiLlama price lookups and, once per form, the blank IRS PDFs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from .. import form1099da
from ..taxforms import EXPORTS
from .data import CATEGORIES, AppData, account_label


def money(v: Any, signed: bool = False, cents: bool = True) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v))
    s = f"{abs(d):,.2f}" if cents else f"{abs(d):,.0f}"
    if d < 0:
        return f"-${s}"
    return f"+${s}" if signed and d > 0 else f"${s}"


def qty(v: Any) -> str:
    d = Decimal(str(v))
    if d == 0:
        return "0"
    if abs(d) >= 1000:
        return f"{d:,.2f}"
    if abs(d) >= 1:
        return f"{d:,.4f}".rstrip("0").rstrip(".")
    return f"{d:.8f}".rstrip("0").rstrip(".")


def price(v: Any) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v))
    if d >= 1:
        return f"${d:,.2f}"
    if d >= Decimal("0.01"):
        return f"${d:,.4f}"
    return f"${d:.8f}".rstrip("0")


def create_app(root: Path, pre: str = "hifo", post: str = "hifo") -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    data = AppData(root, pre, post)
    app.jinja_env.filters.update(money=money, qty=qty, price=price)

    @app.context_processor
    def common() -> dict[str, Any]:
        return {"years": data.years(), "loaded": data.loaded, "methods": (data.pre, data.post)}

    @app.get("/")
    def index() -> Any:
        return redirect(url_for("portfolio"))

    @app.get("/portfolio")
    def portfolio() -> str:
        account = request.args.get("account", "")
        a = data.assets(account)
        tax = []
        for y in data.years():
            s = data.year_summary(y)
            tax.append({"year": y, "gains": s["total"], "income": s["income"]})
        return render_template("portfolio.html", a=a, account=account, accounts=data.accounts(), tax=tax,
                               history=data.history(), show_all=request.args.get("all") == "1")

    @app.get("/tax/<int:year>")
    def tax(year: int) -> str:
        if year not in data.years():
            abort(404)
        s = data.year_summary(year)
        forms = form1099da.load_forms(year)
        tabs = {"irs": "IRS Forms", "software": "Tax Filing Software", "csv": "CSV Reports"}
        return render_template("tax.html", s=s, cats=CATEGORIES, exports=EXPORTS, tabs=tabs, forms=forms)

    @app.get("/tax/<int:year>/export/<key>")
    def export(year: int, key: str) -> Response:
        if key not in {e.key for e in EXPORTS}:
            abort(404)
        name, body = data.export(year, key)
        if not body:
            abort(404, "nothing to report for this export")
        mime = "application/pdf" if name.endswith(".pdf") else "text/csv"
        return Response(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/refresh")
    def refresh() -> Any:
        data.refresh()
        return redirect(request.referrer or url_for("portfolio"))

    # -- 1099-DA ---------------------------------------------------------------------------------
    @app.get("/1099-da")
    def da_list() -> str:
        forms = form1099da.load_forms()
        exch = sorted({a for a, _ in data.accounts() if "|" not in a and not a.startswith(("escrow", "staked",
                                                                                              "deposit", "xp-chain",
                                                                                              "vault", "offlist"))})
        rows = []
        for f in forms:
            r = form1099da.reconcile(f, data.tax_year(f.year).lines) if f.year in data.years() else None
            rows.append((f, r))
        return render_template("da_list.html", rows=rows, exchanges=exch or ["Gemini", "Coinbase", "Crypto.com"],
                               default_year=max(2025, datetime.now(UTC).year - 1))  # forms arrive early next year

    @app.post("/1099-da")
    def da_upload() -> Any:
        f = request.files.get("file")
        if not f or not f.filename:
            abort(400, "choose a file")
        year = int(request.form["year"])
        account = request.form["account"]
        try:
            form = form1099da.import_file(year, account, f.filename, f.read())
        except ValueError as e:
            return render_template("message.html", title="Could not read this 1099-DA", message=str(e)), 400
        data._years.pop(year, None)  # box assignment depends on imported 1099-DAs
        return redirect(url_for("da_detail", year=year, form_id=form.id))

    @app.get("/1099-da/<int:year>/<form_id>")
    def da_detail(year: int, form_id: str) -> str:
        form = next((f for f in form1099da.load_forms(year) if f.id == form_id), None)
        if form is None:
            abort(404)
        r = form1099da.reconcile(form, data.tax_year(year).lines) if year in data.years() else None
        return render_template("da_detail.html", form=form, r=r, label=account_label(form.account, {}))

    @app.post("/1099-da/<int:year>/<form_id>/delete")
    def da_delete(year: int, form_id: str) -> Any:
        form1099da.delete_form(year, form_id)
        data._years.pop(year, None)
        return redirect(url_for("da_list"))

    return app


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, pre: str = "hifo", post: str = "hifo") -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to listen on a non-local address: this app shows private financial data")
    app = create_app(root, pre, post)
    print(f"Portfolio and tax reports: http://{host}:{port}/  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, use_reloader=False)
