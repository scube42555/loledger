from __future__ import annotations

from decimal import Decimal

from loledger.web.transactions import Filters, Tx, build, kind_of, to_csv


def _row(tx: str, ts: int, token: str, amount: str, usd: str = "", cls: str = "SWAP", sub: str = "market",
         role: str = "", chain: str = "Ethereum", wallet: str = "0xabc", gas: str = "") -> dict[str, str]:
    return {"chain": chain, "timestamp": str(ts), "tx_hash": tx, "wallet": wallet, "token": token, "amount": amount,
            "usd_value_at_time": usd, "classification": cls, "subtype": sub, "leg_role": role, "counterparty": "0xdef",
            "reason": "note", "linked_tx_hash": "", "confidence": "1.0", "gas_fee_native": gas,
            "gas_fee_usd": "0.50" if gas else ""}


ROWS = [
    # one order, two fills, plus a fee leg: one transaction worth the sum of a side
    _row("o1", 1_700_000_000, "USDT", "-100", "-100", role="out", chain="Gemini", wallet="gemini:exchange"),
    _row("o1", 1_700_000_000, "BTC", "0.002", "99", role="in", chain="Gemini", wallet="gemini:exchange"),
    _row("o1", 1_700_000_000, "USDT", "-50", "-50", role="out", chain="Gemini", wallet="gemini:exchange"),
    _row("o1", 1_700_000_000, "BTC", "0.001", "49", role="in", chain="Gemini", wallet="gemini:exchange"),
    _row("o1", 1_700_000_000, "BTC", "-0.00001", "-0.5", role="fee", chain="Gemini", wallet="gemini:exchange"),
    _row("r1", 1_710_000_000, "ETH", "0.01", "30", cls="OTHER", sub="staking_reward", role="in", gas="0.0001"),
    _row("s1", 1_720_000_000, "SCAM", "1000", "", cls="OTHER", sub="spam_phishing", role="in"),
]


def _txs() -> list[Tx]:
    return build(ROWS, lambda a: a.upper(), {"Ethereum": "ETH"})


def test_groups_per_tx_and_account_with_kind_and_value() -> None:
    txs = _txs()
    assert [t.tx for t in txs] == ["s1", "r1", "o1"]  # newest first
    s1, r1, o1 = txs
    assert (o1.kind, o1.acct, o1.label) == ("Trade", "Gemini", "GEMINI")
    assert o1.value == Decimal(150) and o1.fee_usd == Decimal("0.5")
    assert [(lg.token, lg.amount) for lg in o1.summary] == [("USDT", Decimal(-150)), ("BTC", Decimal("0.003"))]
    assert (r1.kind, r1.gas_native, r1.gas_symbol, r1.fee_usd) == ("Income", Decimal("0.0001"), "ETH", Decimal("0.50"))
    assert s1.kind == "Spam"


def test_kind_mapping() -> None:
    assert kind_of("INTERNAL_TRANSFER", "to_exchange") == "Own transfer"
    assert kind_of("TRANSFER", "fiat_deposit") == "Fiat deposit"
    assert kind_of("OTHER", "futures_realized_pnl") == "Futures"
    assert kind_of("OTHER", "exchange_withdrawal_fee") == "Fee"
    assert kind_of("ORDER_CANCELLED", "refund") == "Order cancelled"
    assert kind_of("OTHER", "payment") == kind_of("OTHER", "purchased_item") == "Purchase"
    assert kind_of("OTHER", "card_topup") == "Card top-up"


def test_filters() -> None:
    txs = _txs()

    def hits(**a: str) -> list[str]:
        return [t.tx for t in Filters.parse(a).apply(txs)]

    assert hits() == ["r1", "o1"]  # spam hidden by default
    assert hits(spam="1") == ["s1", "r1", "o1"]
    assert hits(kind="Spam") == ["s1"]
    assert hits(asset="btc") == ["o1"]
    assert hits(account="Gemini") == ["o1"]
    assert hits(chain="Ethereum") == ["r1"]
    assert hits(min="100") == ["o1"] and hits(max="$40") == ["r1"]
    assert hits(start="2024-03-09", end="2024-03-09") == ["r1"]  # 1_710_000_000 = 2024-03-09 16:00 UTC
    assert hits(end="2024-03-08") == ["o1"]
    assert hits(q="R1") == ["r1"]
    assert hits(sort="value") == ["o1", "r1"] and hits(sort="old") == ["o1", "r1"]
    assert hits(min="junk", start="nope") == ["r1", "o1"]  # bad input is ignored, not an error


def test_csv_has_one_row_per_leg() -> None:
    lines = to_csv(_txs()).decode().splitlines()
    assert lines[0].startswith("date_utc,chain,account,type")
    assert len(lines) == 1 + 7


def test_same_time_transfer_is_listed_in_chain_order_send_then_receipt() -> None:
    rows = [_row("t", 1_800_000_000, "ADA", "5", "2", cls="INTERNAL_TRANSFER", sub="", role="in", chain="Cardano",
                 wallet="stake1hard"),
            _row("t", 1_800_000_000, "ADA", "-5", "-2", cls="INTERNAL_TRANSFER", sub="", role="out", chain="Cardano",
                 wallet="stake1mid")]
    txs = build(rows, lambda a: a, {})
    assert [t.acct for t in txs] == ["Cardano|stake1hard", "Cardano|stake1mid"]  # newest first: receipt on top
    assert [t.acct for t in Filters.parse({"sort": "old"}).apply(txs)] == ["Cardano|stake1mid", "Cardano|stake1hard"]


def test_card_topup_is_a_disposal_like_a_payment() -> None:
    from loledger.costbasis import Leg, kind

    def leg(sub: str) -> Leg:
        return Leg(0, "Ethereum", 0, "tx", "0xw", "Ethereum|0xw", "USDC", "usdc", Decimal(-50), Decimal(50), "OTHER", sub,
                   "out", "", "")

    assert kind(leg("card_topup")) == kind(leg("payment")) == "dispose"


def test_filters_take_several_values_per_field() -> None:
    from werkzeug.datastructures import MultiDict

    txs = _txs()

    def hits(*pairs: tuple[str, str]) -> list[str]:
        return [t.tx for t in Filters.parse(MultiDict(pairs)).apply(txs)]

    assert hits(("kind", "Trade"), ("kind", "Income")) == ["r1", "o1"]
    assert hits(("kind", "Trade"), ("kind", "Spam")) == ["s1", "o1"]
    assert hits(("chain", "Gemini"), ("chain", "Ethereum")) == ["s1", "r1", "o1"][1:]  # spam hidden by default
    assert hits(("asset", "eth, BTC")) == ["r1", "o1"]  # typed comma list works too
    assert hits(("account", "Gemini"), ("account", "Ethereum|0xabc")) == ["r1", "o1"]


def test_swap_service_and_bridge_transfers_get_their_own_types() -> None:
    assert kind_of("TRANSFER", "exchanger_swap_out") == kind_of("TRANSFER", "exchanger_swap_in") == "Trade"
    assert kind_of("TRANSFER", "bridge_in") == kind_of("INTERNAL_TRANSFER", "bridge_out") == "Bridge"
    assert kind_of("TRANSFER", "to_eoa") == "Transfer"
