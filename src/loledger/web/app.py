"""Local web app: portfolio, accounts (add / re-sync wallets), per-year tax report with exports and Form 1099-DA
import.

Run with `loledger serve` (binds to 127.0.0.1 only). Everything is computed locally from classified.csv
and manual.yaml; the only outbound calls are DefiLlama price lookups and, once per form, the blank IRS PDFs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from .. import form1099da
from ..taxforms import EXPORTS, box_summary
from . import accounts as acc
from .data import CATEGORIES, PARKED, AppData, account_label

TAX_YEAR_COOKIE = "tax_year"  # last tax report viewed: the "Tax reports" menu opens it


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
    syncer = acc.Syncer(root, data.refresh)
    app.jinja_env.filters.update(money=money, qty=qty, price=price)

    @app.context_processor
    def common() -> dict[str, Any]:
        return {"years": data.years(), "loaded": data.loaded, "methods": (data.pre, data.post),
                "syncing": syncer.busy}

    def exchanges() -> list[str]:
        """Exchange accounts with activity (not listed in addresses.csv)."""
        return sorted({a for a, _ in data.accounts() if "|" not in a and not a.startswith(PARKED)})

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
            tax.append({"year": y, "gains": s["total"], "income": s["income"], "carry_in": sum(s["carry_in"]),
                        "deducted": s["deducted"], "carry_out": sum(s["carry_out"])})
        return render_template("portfolio.html", a=a, account=account, accounts=data.accounts(), tax=tax,
                               history=data.history(), show_all=request.args.get("all") == "1")

    # -- accounts ----------------------------------------------------------------------------------
    @app.get("/accounts")
    def accounts(error: str = "", form: dict[str, str] | None = None) -> str:
        values = data.account_values()
        held = data.account_assets()
        wallets = acc.read_wallets(root, data.settings)
        groups: dict[str, list[dict[str, Any]]] = {}
        seen: set[str] = set()
        for w in sorted(wallets, key=lambda w: (w.gift, w.label.lower(), w.address)):
            keys = [k for k in data.wallet_accounts(w.chain, w.address, w.label) if k in values]
            dup = bool(keys) and all(k in seen for k in keys)  # e.g. two addr1 rows of one Cardano wallet
            seen.update(keys)
            v = {"mv": sum((values[k]["mv"] for k in keys), Decimal(0)),
                 "cost": sum((values[k]["cost"] for k in keys), Decimal(0)),
                 "assets": sum(values[k]["assets"] for k in keys)} if not dup and (keys or (w.supported and not w.gift)) \
                else None  # synced but empty -> $0; gift recipients and unsupported chains -> "—"
            assets = [] if dup else sorted((a for k in keys for a in held.get(k, [])),
                                           key=lambda a: (-(a["mv"] or 0), a["symbol"]))
            groups.setdefault(w.chain, []).append({"w": w, "v": v, "dup": dup, "account": keys[0] if keys else "",
                                                   "assets": assets})
        for rs in groups.values():  # largest balance first; gift / unsupported / duplicate rows ("—") last
            rs.sort(key=lambda r: (r["v"] is None, r["w"].gift, -(r["v"]["mv"] if r["v"] else 0),
                                   r["w"].label.lower(), r["w"].address))
        def ex_source(name: str) -> str:
            from ..adapters.exchange import gemini, okx

            c = data.settings.chains.get(name)
            if c is not None and (c.parser == "okx" and okx.has_keys() or c.parser == "gemini" and gemini.has_keys()):
                return f"API (keys in .env) + export files in {c.export}"
            if c is not None and c.export:
                return f"export files in {c.export}"
            return "API (keys in .env)" if c is not None else ""

        closed = data.profile.closed_accounts
        ex: list[dict[str, Any]] = sorted(({"name": e, "v": values.get(e), "assets": held.get(e, []),
                                            "source": ex_source(e), "last": data.last_activity.get(e),
                                            "closed": str((closed[e].get("date") if isinstance(closed[e], dict)
                                                           else closed[e]) if e in closed else "")}
                                           for e in exchanges()),
                                          key=lambda e: (-(e["v"]["mv"] if e["v"] else 0), e["name"]))  # largest first
        chain_mv = {c: sum((r["v"]["mv"] for r in rs if r["v"]), Decimal(0)) for c, rs in groups.items()}
        order = sorted(groups, key=lambda c: (-chain_mv[c], c))
        return render_template("accounts.html", groups=[(c, groups[c], chain_mv[c]) for c in order], exchanges=ex,
                               chains=acc.chain_choices(data.settings), job=syncer.job, error=error,
                               form=form or {}, n_wallets=len(wallets), total=sum(chain_mv.values(), Decimal(0))
                               + sum((e["v"]["mv"] for e in ex if e["v"]), Decimal(0)))

    @app.post("/accounts/add")
    def account_add() -> Any:
        f = {k: request.form.get(k, "").strip() for k in ("chain", "chain_other", "address", "label")}
        chain = f["chain_other"] if f["chain"] == "__other" else f["chain"]
        try:
            chain, address = acc.validate(data.settings, chain, f["address"], acc.read_wallets(root, data.settings))
        except ValueError as e:
            return accounts(error=str(e), form=f), 400
        acc.append_wallet(root, chain, address, f["label"])
        if request.form.get("sync") and not syncer.busy:
            w = next(x for x in acc.read_wallets(root, data.settings) if x.chain == chain and x.address == address)
            if w.supported and not w.gift:
                syncer.sync_one(w)
        return redirect(url_for("accounts"))

    @app.post("/accounts/sync")
    def account_sync() -> Any:
        try:
            if request.form.get("all"):
                syncer.sync_all()
            elif request.form.get("exchange"):
                if request.form["exchange"] not in exchanges():
                    abort(400, "unknown exchange")
                if request.form["exchange"] in data.profile.closed_accounts:
                    abort(400, "this account is closed (tax_profile.yaml closed_accounts)")
                syncer.sync_exchange(request.form["exchange"])
            else:
                chain, address = request.form.get("chain", ""), request.form.get("address", "")
                w = next((x for x in acc.read_wallets(root, data.settings)
                          if x.chain == chain and x.address == address), None)
                if w is None or not w.supported or w.gift:
                    abort(400, "this wallet can't be synced")
                syncer.sync_one(w)
        except RuntimeError as e:
            return accounts(error=str(e)), 409
        return redirect(url_for("accounts"))

    @app.get("/accounts/sync/status")
    def sync_status() -> Response:
        return jsonify(syncer.job.status() if syncer.job else None)

    # -- tax reports --------------------------------------------------------------------------------
    @app.get("/tax")
    def tax_latest() -> Any:
        ys = data.years()
        if not ys:
            return render_template("message.html", title="No tax years yet",
                                   message="No disposals or income found in classified.csv.")
        last = request.cookies.get(TAX_YEAR_COOKIE, "")
        year = int(last) if last.isdigit() and int(last) in ys else max(ys)
        return redirect(url_for("tax", year=year))

    @app.get("/tax/<int:year>")
    def tax(year: int) -> Any:
        if year not in data.years():
            abort(404)
        s = data.year_summary(year)
        forms = [(f, form1099da.reconcile(f, data.tax_year(year).lines)) for f in form1099da.load_forms(year)]
        tabs = {"irs": "IRS Forms", "software": "Tax Filing Software", "csv": "CSV Reports"}
        ys = sorted(data.years())
        i = ys.index(year)
        prev_y, next_y = (ys[i - 1] if i > 0 else None), (ys[i + 1] if i + 1 < len(ys) else None)
        resp = app.make_response(render_template(
            "tax.html", s=s, cats=CATEGORIES, exports=EXPORTS, tabs=tabs, forms=forms, prev_y=prev_y, next_y=next_y,
            proceeds=data.profile.proceeds, boxes=box_summary(data.tax_year(year)),
            da_exchanges=exchanges() or ["Gemini", "Coinbase", "Crypto.com"]))
        resp.set_cookie(TAX_YEAR_COOKIE, str(year), max_age=365 * 86400, samesite="Lax", httponly=True)
        return resp

    @app.get("/tax/<int:year>/export/<key>")
    def export(year: int, key: str) -> Response:
        if key not in {e.key for e in EXPORTS}:
            abort(404)
        name, body = data.export(year, key)
        if not body:
            abort(404, "nothing to report for this export")
        mime = "application/pdf" if name.endswith(".pdf") else "text/csv"
        return Response(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/settings/proceeds")
    def set_proceeds() -> Any:
        choice = request.form.get("proceeds", "records")
        if choice not in ("records", "1099da"):
            abort(400)
        data.set_proceeds(choice)
        return redirect(request.referrer or url_for("portfolio"))

    @app.post("/refresh")
    def refresh() -> Any:
        data.refresh()
        return redirect(request.referrer or url_for("portfolio"))

    # -- 1099-DA (imported from the tax report page of its year) ----------------------------------------
    @app.get("/1099-da")
    def da_list() -> Any:  # old menu link: forms now live on each year's tax report
        ys = [y for y in data.years() if y >= 2025]
        y = max(ys) if ys else max(data.years(), default=datetime.now(UTC).year)
        return redirect(url_for("tax", year=y) + "#da")

    @app.post("/1099-da")
    def da_upload() -> Any:
        f = request.files.get("file")
        if not f or not f.filename:
            abort(400, "choose a file")
        year = int(request.form["year"])
        if year < 2025:
            abort(400, "Form 1099-DA starts with tax year 2025")
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
        return redirect(url_for("tax", year=year) + "#da")

    return app


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, pre: str = "hifo", post: str = "hifo") -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to listen on a non-local address: this app shows private financial data")
    app = create_app(root, pre, post)
    print(f"Portfolio and tax reports: http://{host}:{port}/  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, use_reloader=False)
