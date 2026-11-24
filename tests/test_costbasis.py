"""Cost basis engine: universal FIFO to 2024, 2025-01-01 allocation, per-wallet FIFO after, escrow, fees."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from loledger.costbasis import Engine, Leg, kind

D = Decimal
_n = 0


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def leg(when: str, acct: str, fam: str, amount: str, usd: str | None, cls: str, sub: str = "", *, tx: str = "",
        role: str = "", order: str = "", linked: str = "") -> Leg:
    global _n
    _n += 1
    lg = Leg(_n, "Chain", ts(when), tx or f"tx{_n}", acct, acct, fam.upper(), fam, D(amount),
             None if usd is None else D(usd), cls, sub, role, linked, order)
    lg.kind = kind(lg)
    return lg


def buy(when: str, acct: str, qty: str, usd: str) -> list[Leg]:
    t = f"buy{when}{acct}"
    return [leg(when, acct, "usd", f"-{usd}", usd, "SWAP", "exchange_trade", tx=t),
            leg(when, acct, "btc", qty, usd, "SWAP", "exchange_trade", tx=t)]


def sell(when: str, acct: str, qty: str, usd: str) -> list[Leg]:
    t = f"sell{when}{acct}"
    return [leg(when, acct, "btc", f"-{qty}", usd, "SWAP", "exchange_trade", tx=t),
            leg(when, acct, "usd", usd, usd, "SWAP", "exchange_trade", tx=t)]


def test_universal_fifo_before_2025_then_allocation_then_wallet_fifo() -> None:
    legs = (buy("2024-01-01", "A", "1", "100") + buy("2024-02-01", "B", "1", "200")
            + sell("2024-03-01", "B", "1", "300")  # universal FIFO: sells A's older lot (basis 100)
            + sell("2025-06-01", "A", "1", "400"))  # after allocation A holds the unused 200 lot
    res = Engine(pre="fifo", post="fifo").run(legs)
    d24, d25 = sorted(res.disposals, key=lambda d: d.ts)
    assert (d24.lot.cost, d24.gain, d24.method) == (D(100), D(200), "universal_fifo")
    assert (d25.lot.cost, d25.gain, d25.method, d25.term) == (D(200), D(200), "wallet_fifo", "long")
    assert [(a, lot.cost, how) for a, _f, lot, how in res.allocation] == [("A", D(200), "assigned")]


def test_transfer_carries_basis_and_the_fee_is_a_small_sale() -> None:
    t = "xfer"
    legs = buy("2025-01-02", "A", "1", "100") + [
        leg("2025-02-01", "A", "btc", "-1", "150", "INTERNAL_TRANSFER", tx=t),
        leg("2025-02-01", "B", "btc", "0.99", "148.5", "INTERNAL_TRANSFER", tx=t),
    ] + sell("2025-03-01", "B", "0.99", "200")
    res = Engine().run(legs)
    fee, sale = sorted(res.disposals, key=lambda d: d.ts)
    assert fee.event == "transfer_fee" and fee.lot.qty == D("0.01") and fee.lot.cost == D(1) and fee.proceeds == D("1.5")
    assert sale.acct == "B" and sale.lot.cost == D(99) and sale.lot.acq == ts("2025-01-02")


def test_order_escrow_fill_consumes_only_what_the_refund_does_not_return() -> None:
    legs = buy("2025-01-02", "A", "10", "1000") + [
        leg("2025-02-01", "A", "btc", "-10", "1200", "ORDER_PLACED", "limit", tx="place", role="order", order="o1"),
        leg("2025-02-02", "A", "eth", "3", "900", "ORDER_FILLED", "limit", tx="fill", role="in", order="o1"),
        leg("2025-02-03", "A", "btc", "4", "480", "ORDER_CANCELLED", "limit", tx="cancel", role="refund", order="o1"),
    ]
    res = Engine().run(legs)
    (d,) = res.disposals
    assert d.lot.qty == D(6) and d.lot.cost == D(600) and d.proceeds == D(900)
    held = {k: sum((x.qty for x in v), D(0)) for k, v in res.holdings.items()}
    assert held[("A", "btc")] == D(4) and held[("A", "eth")] == D(3)


def test_sale_processed_before_its_funding_deposit_gets_the_late_basis() -> None:
    legs = buy("2025-01-02", "W", "1", "100") + sell("2025-02-01 10:00:00", "X", "1", "150") + [
        leg("2025-02-01 10:00:05", "W", "btc", "-1", "150", "INTERNAL_TRANSFER", tx="late", linked="dep"),
        leg("2025-02-01 10:00:05", "X", "btc", "1", "150", "INTERNAL_TRANSFER", tx="dep"),
    ]
    res = Engine().run(legs)
    (d,) = res.disposals
    assert d.lot.cost == D(100) and d.lot.flag == "funded_late"
    assert not [h for h in res.holdings.values() if sum((x.qty for x in h), D(0)) > 0]


def test_hifo_sells_the_most_expensive_lot_and_leaves_the_cheap_one_for_2025() -> None:
    legs = (buy("2024-01-01", "A", "1", "100") + buy("2024-02-01", "B", "1", "200")
            + sell("2024-03-01", "A", "1", "300")  # universal HIFO: B's 200 lot, although A sold
            + sell("2025-06-01", "B", "1", "400"))  # B is left holding the unused 100 lot (acquired 2024-01-01)
    res = Engine().run(legs)
    d24, d25 = sorted(res.disposals, key=lambda d: d.ts)
    assert (d24.lot.cost, d24.method) == (D(200), "universal_hifo")
    assert (d25.acct, d25.lot.cost, d25.lot.acq, d25.method) == ("B", D(100), ts("2024-01-01"), "wallet_hifo")


def test_wallet_hifo_after_2025_picks_highest_unit_cost_within_the_wallet() -> None:
    legs = (buy("2025-01-02", "A", "1", "300") + buy("2025-02-01", "A", "1", "100") + buy("2025-03-01", "A", "1", "200")
            + sell("2025-04-01", "A", "2", "500"))
    res = Engine().run(legs)
    assert sorted(d.lot.cost for d in res.disposals) == [D(200), D(300)]


def test_unpriced_token_redeemed_takes_the_residual_proceeds() -> None:
    # LP token (no price) + 4 ADA sent, $75 of priced assets received: ADA keeps its $4, the LP token gets $71
    t = "redeem"
    legs = [leg("2025-01-02", "W", "ada", "10", "10", "SWAP", "exchange_trade", tx="b0"),
            leg("2025-01-02", "W", "lp", "1000", None, "LP_DEPOSIT", "lp", tx="b0"),
            leg("2025-02-01", "W", "lp", "-1000", None, "LP_WITHDRAW", "lp", tx=t),
            leg("2025-02-01", "W", "ada", "-4", "4", "LP_WITHDRAW", "lp", tx=t),
            leg("2025-02-01", "W", "meld", "5000", "75", "LP_WITHDRAW", "lp", tx=t)]
    res = Engine().run(legs)
    got = {d.fam: d.proceeds for d in res.disposals if d.tx == t}
    assert got == {"ada": D(4), "lp": D(71)}


def test_closed_account_written_off_without_a_deduction_by_default() -> None:
    legs = buy("2024-03-01", "MEXC", "100", "100") + sell("2025-02-01", "MEXC", "40", "40") \
        + buy("2025-09-01", "Gemini", "1", "1")
    res = Engine(closures={"MEXC": "2025-08-01"}).run(legs)
    assert not [d for d in res.disposals if d.event == "account_closed"]  # nothing on Form 8949
    assert res.written_off == [(ts("2025-08-01"), "MEXC", "btc", D(60), D(60))]
    assert not res.holdings.get(("MEXC", "btc")) and any(f.what.startswith("left on MEXC") for f in res.flags)
    assert sum(lot.qty for lot in res.holdings[("Gemini", "btc")]) == D(1)  # other accounts untouched


def test_closed_account_as_capital_loss_when_chosen() -> None:
    legs = buy("2024-03-01", "MEXC", "100", "100") + sell("2025-02-01", "MEXC", "40", "40")
    res = Engine(closures={"MEXC": {"date": "2025-08-01", "treatment": "capital_loss"}}).run(legs)
    closed = [d for d in res.disposals if d.event == "account_closed"]
    assert [(d.acct, d.lot.qty, d.proceeds, d.lot.cost) for d in closed] == [("MEXC", D(60), D(0), D(60))]
    assert res.written_off == [] and not res.holdings.get(("MEXC", "btc"))


def test_gift_of_inherited_property_uses_fmv_at_death_and_the_dual_basis_rule() -> None:
    """Inherited 2024-03-15 (FMV $100k), then gifted when worth $60k/unit. Basis $100k for a gain, $60k for a
    loss, no gain or loss in between; always long-term (even sold days after the gift); 8949 says INHERITED."""
    gift = [{"txs": ["g1"], "inherited": "2024-03-15", "basis_per_unit": "100000"}]
    legs = ([leg("2026-06-13", "W", "btc", "1", "60000", "OTHER", "gift_received", tx="g1")]
            + sell("2026-06-15", "W", "0.25", "16000")   # $64k/unit: between -> no gain or loss
            + sell("2026-07-01", "W", "0.25", "27500")   # $110k/unit: gain over the $100k basis
            + sell("2026-08-01", "W", "0.25", "12500"))  # $50k/unit: loss under the $60k gift value
    res = Engine(gifts=gift).run(legs)
    mid, up, down = sorted(res.disposals, key=lambda d: d.ts)
    assert (mid.lot.cost, mid.gain) == (D(16000), D(0))
    assert (up.lot.cost, up.gain) == (D(25000), D(2500))
    assert (down.lot.cost, down.gain) == (D(15000), D(-2500))
    assert {d.term for d in (mid, up, down)} == {"long"}
    held = [lot for lots in res.holdings.values() for lot in lots if lot.qty > 0 and lot.origin.startswith("gift")]
    assert held[0].qty == D("0.25") and held[0].cost == D(25000) and held[0].gift_fmv == D(15000)
    assert any("inherited" in f.what for f in res.flags)

    from loledger.taxforms import Profile, build_year

    (ln,) = [x for x in build_year(res, 2026, Profile()).lines if x.sold == "06/15/2026"]
    assert ln.acquired == "INHERITED" and ln.term == "long"


def test_gift_with_unknown_history_still_uses_fmv() -> None:
    legs = [leg("2026-06-13", "W", "btc", "1", "60000", "OTHER", "gift_received", tx="g2")] + sell(
        "2026-06-15", "W", "1", "64000")
    (d,) = Engine(gifts=[{"txs": ["other"], "inherited": "2024-03-15", "basis_per_unit": "1"}]).run(legs).disposals
    assert (d.lot.cost, d.term) == (D(60000), "short")
