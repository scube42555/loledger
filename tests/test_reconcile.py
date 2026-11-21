"""Custody reconciliation: holdings vs live, negative running balances, boundary-flow pairing."""

from __future__ import annotations

from decimal import Decimal

from loledger.reconcile import Row, boundary, gaps, holdings, real_rows

D = Decimal


def _r(chain: str, ts: int, wallet: str, token: str, amount: str, *, asset: str = "", cls: str = "TRANSFER",
       sub: str = "", usd: str | None = None, price: str | None = "1", gas: str = "0", tx: str = "") -> Row:
    return Row(chain, ts, tx or f"t{ts}{wallet}", wallet, "", token, asset or token, D(amount),
               None if usd is None else D(usd), None if price is None else D(price), cls, sub, D(gas), "", "", "")


def test_holdings_charge_gas_and_drop_fake_tokens_only() -> None:
    rows = real_rows([
        _r("Ethereum", 1, "0xa", "ETH", "1", asset="native", sub="from_exchange"),
        _r("Ethereum", 2, "0xa", "ETH", "-0.5", asset="native", sub="to_eoa", gas="0.01"),
        _r("Ethereum", 3, "0xa", "USDT", "0.001", asset="0xdac", cls="OTHER", sub="spam_address_poisoning"),
        _r("Ethereum", 3, "0xa", "USDT", "5", asset="0xdac", sub="from_eoa"),  # real USDT is priced
        _r("Ethereum", 4, "0xa", "USDT", "-1000", asset="0xfake", cls="OTHER", sub="spam_address_poisoning", price=None),
    ])
    hs = {h.asset: h for h in holdings(rows, {"Ethereum|0xa": {"native": "0.49", "0xdac": "5.001"}})}
    assert hs["native"].qty == D("0.49") and hs["native"].status == "match"
    assert hs["0xdac"].qty == D("5.001") and hs["0xdac"].status == "match"
    assert "0xfake" not in hs


def test_gap_ignores_same_second_ordering_but_flags_real_shortfall() -> None:
    rows = [_r("MEXC", 10, "mexc:spot", "USDT", "-5"), _r("MEXC", 10, "mexc:spot", "USDT", "5"),
            _r("MEXC", 20, "mexc:spot", "USDT", "-10"), _r("MEXC", 30, "mexc:spot", "USDT", "3")]
    gs = gaps(rows)
    assert [(g.low, g.end) for g in gs] == [(D(-10), D(-7))]


def test_boundary_pairs_hops_batched_deposits_and_swap_services() -> None:
    day = 86400
    rows = [
        _r("MEXC", 0, "mexc:spot", "KAS", "-100", sub="withdrawal_to_chain", usd="-10"),
        _r("Kaspa", 400 * day, "kaspa:me", "KAS", "99.9", sub="from_eoa", usd="5"),
        _r("MEXC", 1000 * day, "mexc:spot", "BTC", "-0.01", sub="withdrawal_to_chain", usd="-600"),
        _r("Bitcoin", 1000 * day + 100, "bc1me", "BTC", "0.004", sub="from_exchange", usd="240"),
        _r("Bitcoin", 1000 * day + 900, "bc1me", "BTC", "0.0059", sub="from_exchange", usd="354"),
        _r("Ethereum", 2000 * day, "0xa", "USDC", "-1000", sub="to_eoa", usd="-1000"),
        _r("OKX", 2000 * day + 700, "okx:account", "USDT", "999", sub="deposit_from_chain", usd="999"),
    ]
    fl, dust = boundary(rows, D(1))
    pairs = {f.row.token: (f.pair.token if f.pair else None, f.pair_kind) for f in fl if f.outflow}
    assert pairs["KAS"][0] == "KAS" and "hop" in pairs["KAS"][1]
    assert pairs["BTC"][0] == "BTC" and "2 deposits" in pairs["BTC"][1]
    assert pairs["USDC"][0] == "USDT" and "swap" in pairs["USDC"][1]
    assert dust == 0
