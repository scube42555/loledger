"""Crypto.com Exchange: signing, day-window fetching, journal normalization and tx-hash linking."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from loledger.adapters.exchange import cryptocom
from loledger.adapters.exchange.cryptocom import CryptoComBundle, CryptoComClient, normalize, param_string, sign
from loledger.decode.exchanges import classify_exchange, link_exchange_transfers
from loledger.http import HttpClient
from loledger.model import AssetKind, Movement, TxContext
from loledger.rules import Classified, Cls
from loledger.store import Store


def test_param_string_sorts_and_flattens() -> None:
    assert param_string({"b": 2, "a": "x", "c": None}) == "axb2cnull"
    assert param_string({"orders": [{"z": 1, "y": 2}, {"x": 3}], "a": 1}) == "a1ordersy2z1x3"
    assert param_string({}) == ""


def test_signature_is_hmac_sha256_of_method_id_key_params_nonce() -> None:
    expect = hmac.new(b"sec", b"private/get-trades7KEYend_time2limit100start_time19", hashlib.sha256).hexdigest()
    assert sign("sec", "private/get-trades", 7, "KEY", {"start_time": 1, "end_time": 2, "limit": 100}, 9) == expect


class FakeCdc:
    """Returns the given rows filtered by [start_time, end_time); caps at `cap` like the real API."""

    def __init__(self, rows: list[dict[str, Any]], cap: int = 100) -> None:
        self.rows, self.cap, self.calls = rows, cap, 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        p = body["params"]
        data = [r for r in self.rows if p["start_time"] <= r["event_timestamp_ms"] < p["end_time"]][: self.cap]
        return httpx.Response(200, json={"code": 0, "result": {"data": data}})


def test_daily_windows_split_full_days_and_cache_past_days(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTOCOM_API_KEY", "k")
    monkeypatch.setenv("CRYPTOCOM_API_SECRET", "s")
    monkeypatch.setattr(cryptocom, "LIMIT", 3)
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % cryptocom.DAY_MS
    rows = [{"journal_id": str(i), "event_timestamp_ms": t0 + i * 1000} for i in range(7)]  # 7 rows in one day
    fake = FakeCdc(rows, cap=3)
    store = Store(tmp_path / "c.sqlite")
    http = HttpClient(0, client=httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None)
    client = CryptoComClient(store, http)
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: (t0 + 5 * cryptocom.DAY_MS) / 1000)
    got = client.daily("private/get-transactions", t0, "journal_id")
    assert [r["journal_id"] for r in got] == [str(i) for i in range(7)]  # nothing lost despite the cap
    calls = fake.calls
    client.daily("private/get-transactions", t0, "journal_id")
    assert fake.calls - calls <= 3  # only the last two (non-final) days are re-read


def _journal(jid: str, jt: str, cur: str, qty: str, ts: int, order: str = "") -> dict[str, Any]:
    return {"journal_id": jid, "journal_type": jt, "instrument_name": cur, "transaction_qty": qty,
            "event_timestamp_ms": ts, "order_id": order, "trade_id": order}


def test_normalize_trades_fees_and_withdrawal_fee_split() -> None:
    b = CryptoComBundle(
        journal=[_journal("1", "TRADING", "USDT", "-831.42", 1000, "o1"), _journal("2", "TRADING", "BCH", "1.5", 1000, "o1"),
                 _journal("3", "TRADE_FEE", "BCH", "-0.00375", 1000, "o1"),
                 _journal("4", "ONCHAIN_WITHDRAWAL", "BTC", "-0.20298456", 2000),
                 _journal("5", "ONCHAIN_DEPOSIT", "USDT", "2999.968445", 3000)],
        trades=[{"order_id": "o1", "instrument_name": "BCH_USDT"}],
        withdrawals=[{"currency": "BTC", "amount": 0.20258456, "fee": 0.0004, "create_time": 2000, "txid": "abc/8", "network_id": "BTC"}],
        deposits=[{"currency": "USDT", "amount": 2999.968445, "fee": 0.0, "create_time": 3000, "txid": "0xDEAD/410", "network_id": "ETH"}],
    )
    moves, txs = normalize(b)
    assert sum((m.amount for m in moves if m.asset_id == "BTC"), Decimal(0)) == Decimal("-0.20298456")
    wd = [m for m in moves if m.asset_id == "BTC"]
    assert sorted((m.source, m.amount) for m in wd) == [("fee", Decimal("-0.0004")), ("onchain_withdrawal", Decimal("-0.20258456"))]
    dep_tx = next(t for t in txs.values() if t.meta.get("journal_type") == "ONCHAIN_DEPOSIT")
    assert (dep_tx.meta["txid"], dep_tx.meta["txid_index"]) == ("0xdead", "410")
    rows = classify_exchange(moves, txs, "crypto.com")
    trade = [r for r in rows if r.classification == Cls.SWAP]
    assert {r.leg_role for r in trade} == {"in", "out", "fee"}
    assert "sold 831.42 USDT for 1.5 BCH" in trade[0].order_details


def _onchain(h: str, leg: str, amount: str, sym: str = "USDT") -> Classified:
    tx = TxContext(chain="Ethereum", tx_hash=h, block=1, timestamp=1, tx_from="0xw", tx_to="0xt", method_id="",
                   function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0))
    m = Movement(chain="Ethereum", tx_hash=h, leg_id=leg, block=1, timestamp=1, wallet="0xw", asset_id="0xusdt",
                 asset_kind=AssetKind.ERC20, symbol=sym, decimals=6, amount=Decimal(amount), counterparty="0xdep",
                 source="tokentx")
    return Classified(m, tx, Cls.TRANSFER, "to_eoa", "", 1.0, "")


def test_deposit_linked_by_tx_hash_and_log_index_dust_flagged() -> None:
    b = CryptoComBundle(journal=[_journal("5", "ONCHAIN_DEPOSIT", "USDT", "100", 3000),
                                 _journal("6", "ONCHAIN_DEPOSIT", "USDT", "0.000999", 4000)],
                        deposits=[{"currency": "USDT", "amount": 100.0, "fee": 0, "create_time": 3000, "txid": "0xabc/7"},
                                  {"currency": "USDT", "amount": 0.000999, "fee": 0, "create_time": 4000, "txid": "0xfff/1"}])
    moves, txs = normalize(b)
    rows = classify_exchange(moves, txs, "crypto.com")
    decoy = _onchain("0xabc", "log:3", "-100")  # same tx, other leg (e.g. a spoofed transfer)
    real = _onchain("0xabc", "log:7", "-100")
    all_rows = [*rows, decoy, real]
    [note] = link_exchange_transfers(all_rows)
    assert real.classification == Cls.INTERNAL_TRANSFER and real.subtype == "to_exchange:crypto.com"
    assert decoy.classification == Cls.TRANSFER
    dep = next(r for r in rows if r.movement.amount == 100)
    assert dep.classification == Cls.INTERNAL_TRANSFER and dep.linked_tx_hash == "0xabc"
    dust = next(r for r in rows if r.movement.amount < 1)
    assert dust.subtype == "spam_dust_deposit"


def test_mexc_network_suffix_split() -> None:
    from loledger.adapters.exchange.files import split_network
    assert split_network("AVAX-CCHAIN") == ("AVAX", "CCHAIN")
    assert split_network("USDT-SOL") == ("USDT", "SOL")
    assert split_network("BTC") == ("BTC", "")
    assert split_network("BTC-USDT") == ("BTC-USDT", "")  # a pair, not a network


def test_gemini_parser_running_balance_and_totals(tmp_path: Any) -> None:
    from pathlib import Path

    from loledger.adapters.exchange.files import parse_gemini
    hdr = ["Date", "Time (UTC)", "Type", "Symbol", "Specification", "USD Amount USD", "Fee (USD) USD", "USD Balance USD",
           "ETH Amount ETH", "Fee (ETH) ETH", "ETH Balance ETH", "Trade ID", "Order ID", "Tx Hash",
           "Deposit Tx Output", "Withdrawal Tx Output", "Withdrawal Destination", "Deposit Destination"]
    def row(**kw: str) -> list[str]:
        return [kw.get(h, "") for h in hdr]
    lines = [hdr,
             row(**{"Date": "2022-05-13", "Time (UTC)": "13:00:00.000", "Type": "Credit", "Symbol": "USD",
                    "Specification": "Deposit (Wire Transfer)", "USD Amount USD": "$5,000.00 ", "USD Balance USD": "$5,000.00 "}),
             row(**{"Date": "2022-05-13", "Time (UTC)": "14:00:00.000", "Type": "Buy", "Symbol": "ETHUSD", "Specification": "Web",
                    "USD Amount USD": "($4,925.50)", "Fee (USD) USD": "($74.50)", "USD Balance USD": "$0.00 ",
                    "ETH Amount ETH": "2.307036 ETH ", "ETH Balance ETH": "2.307036 ETH ", "Order ID": "1"}),
             row(**{"Date": "2022-05-14", "Time (UTC)": "10:00:00.000", "Type": "Debit", "Symbol": "ETH",
                    "Specification": "Withdrawal (ETH)", "ETH Amount ETH": "(2.0 ETH)", "Fee (ETH) ETH": "0.0 ETH ",
                    "ETH Balance ETH": "0.307036 ETH ", "Tx Hash": "c80033657c92"}),
             row(**{"USD Balance USD": "$0.00 ", "ETH Balance ETH": "0.307036 ETH "})]
    p = Path(tmp_path) / "g.csv"
    import csv as _csv
    with p.open("w", newline="") as f:
        _csv.writer(f).writerows(lines)
    led = parse_gemini(p)
    assert "differ beyond display rounding on 0" in led.checks[0] and led.checks[1].endswith("all match")
    wd = next(t for t in led.txs.values() if t.meta["journal_type"] == "ONCHAIN_WITHDRAWAL")
    assert wd.meta["txid"] == "c80033657c92"
    assert sum((m.amount for m in led.moves if m.asset_id == "USD"), Decimal(0)) == 0


# ---- OKX / Binance.US / Coinbase and linking without tx hashes -------------------------------------

def _x_row(chain: str, sub: str, amount: str, sym: str, ts: int, **meta: Any) -> Classified:
    tx = TxContext(chain=chain, tx_hash=f"{chain}:{sub}:{ts}", block=0, timestamp=ts, tx_from="x", tx_to=chain.lower(),
                   method_id="", function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0), meta=dict(meta))
    m = Movement(chain=chain, tx_hash=tx.tx_hash, leg_id="l", block=0, timestamp=ts, wallet=f"{chain.lower()}:account",
                 asset_id=sym, asset_kind=AssetKind.EXCHANGE, symbol=sym, decimals=8, amount=Decimal(amount),
                 counterparty=chain.lower(), source="x")
    return Classified(m, tx, Cls.TRANSFER, sub, chain.lower(), 1.0, "", "in" if Decimal(amount) > 0 else "out")


def _wallet_leg(wallet: str, h: str, amount: str, sym: str, ts: int, cp: str = "0xcp") -> Classified:
    tx = TxContext(chain="Ethereum", tx_hash=h, block=1, timestamp=ts, tx_from=wallet, tx_to=cp, method_id="",
                   function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0))
    m = Movement(chain="Ethereum", tx_hash=h, leg_id="log:1", block=1, timestamp=ts, wallet=wallet, asset_id="0xt",
                 asset_kind=AssetKind.ERC20, symbol=sym, decimals=6, amount=Decimal(amount), counterparty=cp,
                 source="tokentx")
    return Classified(m, tx, Cls.TRANSFER, "to_eoa" if Decimal(amount) < 0 else "from_eoa", "", 1.0, "")


def test_label_and_deposit_address_hints_link_hashless_deposits() -> None:
    from loledger.decode.exchanges import link_exchange_heuristic
    okx_addr = "0xb78eb4f13021350b3190876bc701014b4ab336df"
    cb = _x_row("Coinbase", "deposit_from_chain", "202", "USDC", 10_000, wallet_label="ETH (Tangem)", txid="",
                time_zone_unknown=True)
    okx = _x_row("OKX", "deposit_from_chain", "50", "USDT", 20_300, txid="")
    send_cb = _wallet_leg("0xtangem", "0xa", "-202", "USDC", 10_000 + 5 * 3600)  # local time 5h behind
    send_okx = _wallet_leg("0xtangem", "0xb", "-50", "USDT", 20_000, cp=okx_addr)
    stray = _wallet_leg("0xother", "0xc", "-50", "USDT", 20_100)  # same amount, no address hint
    notes = link_exchange_heuristic([cb, okx, send_cb, send_okx, stray], {"okx": {okx_addr}}, {"0xtangem": "ETH (Tangem)"})
    assert len(notes) == 2
    assert send_cb.linked_tx_hash == cb.tx.tx_hash and send_cb.subtype == "to_exchange:coinbase"
    assert send_okx.linked_tx_hash == okx.tx.tx_hash and stray.linked_tx_hash == ""
    assert 0.6 < okx.confidence < 1.0


def test_exchange_named_destination_only_links_that_wallet() -> None:
    from loledger.decode.exchanges import link_exchange_heuristic
    gem = _x_row("Gemini", "withdrawal_to_chain", "-35", "ETH", 1000, txid="cd427d", address="0xdest")
    elsewhere = _wallet_leg("0xother", "0x1", "35", "ETH", 1200)
    assert link_exchange_heuristic([gem, elsewhere], {}, {}) == []
    right = _wallet_leg("0xdest", "0x2", "35", "ETH", 1270)
    [note] = link_exchange_heuristic([gem, elsewhere, right], {}, {})
    assert right.linked_tx_hash == gem.tx.tx_hash and "destination = this wallet" in gem.reason


def test_txid_without_destination_never_links_a_different_tx() -> None:
    from loledger.decode.exchanges import link_exchange_heuristic
    x = _x_row("MEXC", "withdrawal_to_chain", "-10", "ETH", 1000, txid="0xdeadbeef")
    other = _wallet_leg("0xw", "0x99", "9.99", "ETH", 1100)
    assert link_exchange_heuristic([x, other], {}, {}) == []


def test_exchange_to_exchange_same_txid_is_exact() -> None:
    w = _x_row("Gemini", "withdrawal_to_chain", "-37", "SOL", 1000, txid="2mHw6CxkKnCQts")
    d = _x_row("MEXC", "deposit_from_chain", "37", "SOL", 1100, txid="2mHw6CxkKnCQts")
    notes = link_exchange_transfers([w, d])
    assert d.classification == Cls.INTERNAL_TRANSFER and d.linked_tx_hash == w.tx.tx_hash and notes


def test_coinbase_manual_dates_with_unquoted_comma(tmp_path: Any) -> None:
    from pathlib import Path

    from loledger.adapters.exchange.files import parse_coinbase_manual
    p = Path(tmp_path) / "d.csv"
    p.write_text("Date,Type,From Label,Amount,Asset\nSep 13, 2026 07:46 PM,Deposit,ETH (Tangem),202.00,USDC\n")
    led = parse_coinbase_manual(p)
    [m] = led.moves
    assert (m.asset_id, m.amount) == ("USDC", Decimal("202.00"))
    assert next(iter(led.txs.values())).meta["wallet_label"] == "ETH (Tangem)"


def test_base58_hashes_keep_their_case() -> None:
    from loledger.adapters.exchange.files import _hex_lower, _split_txid
    assert _hex_lower("2mHw6CxkKnCQts") == "2mHw6CxkKnCQts"
    assert _hex_lower("0xABCDEF") == "0xabcdef"
    assert _split_txid("0xAbC/410") == ("0xabc", "410")


def test_flat_withdrawal_fee_allowance_for_small_amounts() -> None:
    from loledger.decode.exchanges import link_exchange_heuristic
    okx = _x_row("OKX", "withdrawal_to_chain", "-10.69", "USDT", 1000, txid="")
    gem = _x_row("Gemini", "deposit_from_chain", "10", "USDT", 1600, txid="")
    too_far = _x_row("Binance.US", "deposit_from_chain", "8", "USDT", 1600, txid="")  # 2.69 > 1.5 flat fee
    notes = link_exchange_heuristic([okx, gem, too_far], {}, {})
    assert len(notes) == 1 and gem.linked_tx_hash == okx.tx.tx_hash and too_far.linked_tx_hash == ""


def test_okx_overlapping_exports_are_counted_once(tmp_path: Path) -> None:
    from loledger.adapters.exchange.files import parse_okx

    head = "UID:1,Account Type:Trading,Time Zone:UTC0\n"
    trade_cols = "id,Order id,Time,Symbol,Balance Change,Fee,Fee Unit,Balance Unit,Balance\n"
    fund_cols = "id,Time,Type,Symbol,Amount,Before Balance\n"
    (tmp_path / "trading_old.csv").write_text(head + trade_cols + "1,o1,2026-11-01 10:00:00,BTC-USDT,0.01,0,BTC,BTC,0.01\n")
    (tmp_path / "trading_new.csv").write_text(head + trade_cols + "1,o1,2026-11-01 10:00:00,BTC-USDT,0.01,0,BTC,BTC,0.01\n"
                                              "2,o2,2026-11-20 10:00:00,BTC-USDT,0.02,0,BTC,BTC,0.03\n")
    (tmp_path / "funding_old.csv").write_text(head + fund_cols + "9,2026-10-01 00:00:00,Deposit,USDT,100,0\n")
    (tmp_path / "funding_new.csv").write_text(head + fund_cols + "9,2026-10-01 00:00:00,Deposit,USDT,100,0\n")
    ledger = parse_okx(tmp_path)
    btc = sum((m.amount for m in ledger.moves if m.symbol == "BTC"), Decimal(0))
    usdt = sum((m.amount for m in ledger.moves if m.symbol == "USDT"), Decimal(0))
    assert (btc, usdt) == (Decimal("0.03"), Decimal("100"))
    assert any("2 row(s) appear in more than one export" in c for c in ledger.checks)
