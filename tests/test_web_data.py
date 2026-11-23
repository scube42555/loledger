from __future__ import annotations

from decimal import Decimal

from loledger.costbasis import Leg
from loledger.web.data import fiat_balances


def _leg(acct: str, fam: str, amount: str) -> Leg:
    return Leg(0, acct, 0, "tx", "w", acct, fam.upper(), fam, Decimal(amount), None, "SWAP", "exchange_trade",
               "", "", "")


def test_usd_cash_per_exchange_from_its_usd_legs() -> None:
    legs = [_leg("Gemini", "usd", "1000"), _leg("Gemini", "usd", "-400.50"), _leg("Gemini", "btc", "1"),
            _leg("Kraken", "usd", "0.004"),  # dust
            _leg("MEXC", "usd", "5"),  # closed
            _leg("Coinbase", "usd", "100"), _leg("Coinbase", "usd", "-100")]  # spent
    assert fiat_balances(legs, {"MEXC": "2025-08-01"}) == {"Gemini": Decimal("599.50")}
