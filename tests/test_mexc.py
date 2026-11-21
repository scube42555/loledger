"""MEXC file parser: 'export all' (UTC+8) plus website downloads (UTC-4) and the futures order history."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from loledger.adapters.exchange.files import parse_mexc

Q = '"'


def _csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.write_text("\n".join(",".join(f"{Q}{c}{Q}" for c in r) for r in [header, *rows]) + "\n")


def _utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def _export_all(d: Path) -> None:
    _csv(d / "x_spot.csv", ["member_id", "symbol", "create_time", "side", "source", "currency", "quantity", "price",
                            "amount_usdt", "fee_currency", "fee", "fee_usdt", "liquidity", "trade_type", "dt"],
         [["m", "ADA-USDT", "2023-05-08 09:44:25", "BUY", "WEB", "ADA", "500", "0.4", "200", "USDT", "0", "0", "", "", ""]])
    _csv(d / "x_deposit.csv", ["member_id", "status", "tes", "create_time", "update_time", "currency", "amount",
                               "actual_amount", "fee", "txid"],
         [["m", "SMALL", "", "2024-04-19 09:33:51", "", "ADA", "1.05", "1.05", "0", "abc:0"]])
    for name in ("withdraw", "transfer", "contract"):
        (d / f"x_{name}.csv").write_text("")
    _csv(d / "x_contract.csv", ["digital_id", "id", "order_id", "position_id", "contract_name", "create_time", "side_en",
                                "leverage", "deal_vol", "deal_avg_price", "fee_currency", "fee", "profit"],
         [["1", "1", "o1", "p1", "ETH_USD", "2023-07-14 04:58:25", "ASK_CLOSE_LONG", "100", "50", "1992", "ETH",
           "0.00002", "-0.001"]])


def test_website_trades_fill_the_gap_before_export_all_and_keep_identical_fills(tmp_path: Path) -> None:
    _export_all(tmp_path)
    hdr = ["Pairs", "Time", "Side", "Filled Price", "Executed Amount", "Total", "Fee", "Role"]
    early = ["FTM_USDT", "2023-02-05 08:50:10", "BUY", "0.5945", "84", "49.938", "0", "Maker"]
    late = ["ADA_USDT", "2023-05-07 21:44:25", "BUY", "0.4", "500", "200", "0", "Maker"]  # = the export-all fill
    _csv(tmp_path / "mexc_Trade-History.csv", hdr, [early, early, late])  # two genuine identical fills
    _csv(tmp_path / "mexc_Trade-History-ALL.csv", hdr, [early, early, ["ADA-USDT", *late[1:]]])  # merged copy
    led = parse_mexc(tmp_path, 8)
    ftm = [m for m in led.moves if m.asset_id == "FTM"]
    assert [m.amount for m in ftm] == [Decimal(84), Decimal(84)]
    assert {m.timestamp for m in ftm} == {_utc("2023-02-05 12:50:10")}  # UTC-4 -> UTC
    assert sum(m.amount for m in led.moves if m.asset_id == "ADA") == 500  # not double-counted


def test_export_all_txid_wins_over_an_edited_website_deposit(tmp_path: Path) -> None:
    _export_all(tmp_path)
    _csv(tmp_path / "mexcDeposit History-ALL.csv", ["Status", "Time", "Crypto", "Network", "Deposit Amount", "TxID"],
         [["Credited Successfully", "2024-04-19 09:33:51", "ADA", "Cardano(ADA)", "1.05", "abc:0"],
          ["Credited Successfully", "2022-12-05 21:49:04", "ETH", "", "1", "0xdef"]])
    led = parse_mexc(tmp_path, 8)
    deps = [m for m in led.moves if m.source == "onchain_deposit"]
    assert [(m.asset_id, m.amount, m.timestamp) for m in deps] == [("ETH", Decimal(1), _utc("2022-12-06 01:49:04"))]


def test_futures_order_history_books_pnl_and_fees_once(tmp_path: Path) -> None:
    _export_all(tmp_path)
    hdr = ["Time (UTC+8)", "Futures Trading Pair", "Direction", "Leverage", "Order Type", "Order Quantity",
           "Filled Quantity", "Unit", "Order Price", "Average Filled Price", "Closing PNL", "Trading Fee",
           "Fee-payment Crypto", "Status", "", ""]
    _csv(tmp_path / "MEXC running list of Futures Order History.csv", hdr, [
        ["2022-12-06 12:18:14", "ETH_USD", "sell long", "0", "MARKET", "249", "249", "Cont", "1257", "1257",
         "-0.0139", "0.0011", "ETH", "FINISHED", "#DIV/0!", "Err:522"],
        ["2023-07-14 04:58:25", "ETH_USD", "sell long", "0", "LIMIT", "50", "50", "Cont", "1992", "1992",
         "-0.001", "0.00002", "ETH", "FINISHED", "", ""],  # also in the contract export
        ["", "", "", "", "", "", "", "", "", "", "-0.86", "", "", "", "", ""]])  # spreadsheet total row
    led = parse_mexc(tmp_path, 8)
    fut = sum((m.amount for m in led.moves if m.wallet == "mexc:futures"), Decimal(0))
    assert fut == Decimal("-0.0139") - Decimal("0.0011") + Decimal("-0.001") - Decimal("0.00002")
