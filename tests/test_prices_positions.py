"""Price service (fake HTTP), enrichment fallbacks and the position ledger."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from txclass.config import load_settings
from txclass.enrich import apply_prices
from txclass.http import HttpClient
from txclass.model import NATIVE_ASSET, AssetKind, Movement, TxContext
from txclass.positions import build_positions
from txclass.prices import PriceService, hour
from txclass.rules import Classified, Cls
from txclass.store import Store

CHAINS = load_settings().chains
USDC = "0x" + "c1" * 20
SNEK = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f.534e454b"
LP = "aa" * 28 + ".4c50"
T0 = 1_714_000_000


class FakeLlama:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        coins = json.loads(request.url.params["coins"])
        out = {c: {"prices": [{"timestamp": ts + 60, "price": self.prices[c]} for ts in tss]}
               for c, tss in coins.items() if c in self.prices}
        return httpx.Response(200, json={"coins": out})

    def http(self) -> HttpClient:
        return HttpClient(0, client=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda s: None)


def row(chain: str, h: str, ts: int, asset: str, amount: str, sym: str, *, kind: AssetKind = AssetKind.ERC20,
        cls: Cls = Cls.TRANSFER, sub: str = "", dec: int = 6, gas: str | None = None, order_id: str = "",
        details: str = "", source: str = "utxo", minted: list[dict[str, Any]] | None = None, protocol: str = "") -> Classified:
    tx = TxContext(chain=chain, tx_hash=h, block=ts, timestamp=ts, tx_from="w", tx_to="v", method_id="",
                   function_name="", is_error=False, gas_fee=Decimal(gas or 0), value=Decimal(0),
                   meta={"minted": minted or []})
    m = Movement(chain=chain, tx_hash=h, leg_id="l", block=ts, timestamp=ts, wallet="w", asset_id=asset,
                 asset_kind=kind, symbol=sym, decimals=dec, amount=Decimal(amount), counterparty="c", source=source)
    r = Classified(m, tx, cls, sub, protocol, 1.0, "")
    r.gas_fee_native = Decimal(gas) if gas else None
    r.order_id, r.order_details = order_id, details
    return r


def test_price_service_batches_and_caches(tmp_path: Any) -> None:
    store = Store(tmp_path / "p.sqlite")
    fake = FakeLlama({"avax:" + USDC: 1.0, "coingecko:avalanche-2": 40.0})
    rows = [row("Avalanche", "0x1", T0, USDC, "-5", "USDC", gas="0.01"),
            row("Avalanche", "0x2", T0 + 10, "0x" + "f0" * 20, "3", "MEME")]
    stats = apply_prices(rows, CHAINS, PriceService(store, fake.http()))
    assert rows[0].usd_value == Decimal(-5) and rows[0].gas_fee_usd == Decimal("0.40")
    assert rows[1].usd_value is None and stats == {"defillama": 1, "unpriced": 1}
    calls = fake.calls
    apply_prices(rows, CHAINS, PriceService(store, fake.http()))  # second run: cache only
    assert fake.calls == calls


def test_alias_fallback_and_spam_skipped(tmp_path: Any) -> None:
    weth = "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab"
    fake = FakeLlama({"coingecko:ethereum": 3000.0})
    rows = [row("Avalanche", "0x1", T0, weth, "0.5", "WETH.e", dec=18),
            row("Avalanche", "0x2", T0, USDC, "-10388", "UЅDС", cls=Cls.OTHER, sub="spam_fake_token")]
    apply_prices(rows, CHAINS, PriceService(Store(tmp_path / "p.sqlite"), fake.http()),
                 {"Avalanche": {weth: "coingecko:ethereum"}})
    assert (rows[0].usd_value, rows[0].price_source) == (Decimal(1500), "defillama:coingecko:ethereum")
    assert rows[1].usd_value is None


def test_cardano_implied_price_only_within_three_days(tmp_path: Any) -> None:
    fake = FakeLlama({"coingecko:cardano": 0.5})
    fill = row("Cardano", "f", T0, SNEK, "1000", "SNEK", kind=AssetKind.CARDANO, dec=0, cls=Cls.ORDER_FILLED,
               details="received 1000 SNEK for 2 ADA; executed 0.002 ADA/SNEK vs limit 0.0021 ADA/SNEK")
    near = row("Cardano", "n", T0 + 86_400, SNEK, "-10", "SNEK", kind=AssetKind.CARDANO, dec=0)
    far = row("Cardano", "x", T0 + 30 * 86_400, SNEK, "-10", "SNEK", kind=AssetKind.CARDANO, dec=0)
    apply_prices([fill, near, far], CHAINS, PriceService(Store(tmp_path / "p.sqlite"), fake.http()))
    assert near.usd_price == Decimal("0.001") and near.price_source.startswith("implied:own_fill")
    assert far.usd_price is None


def test_cardano_lp_position_cost_basis_and_realized_pnl() -> None:
    lp_mint = [{"asset": LP, "qty": "500"}]
    rows = [
        row("Cardano", "o1", T0, SNEK, "-1000", "SNEK", kind=AssetKind.CARDANO, dec=0, cls=Cls.ORDER_PLACED, order_id="A"),
        row("Cardano", "o1", T0, NATIVE_ASSET, "-104", "ADA", kind=AssetKind.NATIVE, cls=Cls.ORDER_PLACED, order_id="A"),
        row("Cardano", "d1", T0 + 60, LP, "500", "LP", kind=AssetKind.CARDANO, dec=0, cls=Cls.LP_DEPOSIT,
            order_id="A", minted=lp_mint, protocol="vyfi"),
        row("Cardano", "d1", T0 + 60, SNEK, "10", "SNEK", kind=AssetKind.CARDANO, dec=0, cls=Cls.LP_DEPOSIT,
            order_id="A", minted=lp_mint, protocol="vyfi"),  # change returned: not the LP token
        row("Cardano", "o2", T0 + 99, LP, "-500", "LP", kind=AssetKind.CARDANO, dec=0, cls=Cls.ORDER_PLACED, order_id="B"),
        row("Cardano", "w1", T0 + 120, SNEK, "900", "SNEK", kind=AssetKind.CARDANO, dec=0, cls=Cls.LP_WITHDRAW,
            order_id="B", minted=[{"asset": LP, "qty": "-500"}], protocol="vyfi"),
        row("Cardano", "w1", T0 + 120, NATIVE_ASSET, "110", "ADA", kind=AssetKind.NATIVE, cls=Cls.LP_WITHDRAW,
            order_id="B", minted=[{"asset": LP, "qty": "-500"}], protocol="vyfi"),
    ]
    prices = {SNEK: Decimal("0.1"), NATIVE_ASSET: Decimal("1")}
    for r in rows:
        if r.movement.asset_id in prices:
            r.usd_value = r.movement.amount * prices[r.movement.asset_id]
    [p] = build_positions(rows)
    s = p.summary()
    assert (s["lp_token"], s["status"], s["lp_in"], s["lp_out"]) == ("LP", "closed", "500", "500")
    assert s["deposited"] == "104 ADA, 990 SNEK" and s["withdrawn"] == "110 ADA, 900 SNEK"
    assert (s["cost_basis_usd"], s["withdrawn_usd"], s["realized_pnl_usd"]) == ("203.00", "200.00", "-3.00")


def test_yield_vault_position() -> None:
    rows = [row("Avalanche", "a", T0, USDC, "-100", "USDC", cls=Cls.OTHER, sub="yield_deposit", protocol="tangem-aave-v3"),
            row("Avalanche", "b", T0 + 9, USDC, "101.5", "USDC", cls=Cls.OTHER, sub="yield_withdraw", protocol="tangem-aave-v3")]
    for r in rows:
        r.usd_value = r.movement.amount
    [p] = build_positions(rows)
    assert p.summary()["realized_pnl_usd"] == "1.50" and p.summary()["status"] == "closed"


def test_hour_bucket_is_stable() -> None:
    assert hour(T0) == hour(T0 + 100) and hour(T0) != hour(T0 + 3600)
