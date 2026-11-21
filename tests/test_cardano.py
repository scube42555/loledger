"""Cardano adapter + order lifecycle, on hand-built Koios-shaped txs (no network)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from loledger.adapters.cardano.address import CHARSET, parse_address
from loledger.adapters.cardano.fetch import CardanoFetcher, CardanoRawBundle
from loledger.adapters.cardano.normalize import normalize
from loledger.config import load_settings
from loledger.decode.cardano_orders import CardanoState, ScriptInfo, classify_cardano_wallet, finalize_orders
from loledger.registry import Registry
from loledger.rules import Classified, Cls, RuleContext
from loledger.store import Store

CHAIN = load_settings().chains["Cardano"]
S = "stake1uwallet"
OTHER_STAKE = "stake1uother"
SNEK = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f.534e454b"
ORDER_SCRIPT = "ab" * 28


def bech(header: int, cred: str = "11" * 28, stake: str = "22" * 28) -> str:
    data = bytes([header]) + bytes.fromhex(cred) + bytes.fromhex(stake)
    acc, bits, out = 0, 0, []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(CHARSET[(acc >> bits) & 31])
    if bits:
        out.append(CHARSET[(acc << (5 - bits)) & 31])
    return "addr1" + "".join(out) + "qqqqqq"


WALLET_ADDR = bech(0x01, "11" * 28)
EOA_ADDR = bech(0x01, "33" * 28)
BATCHER_ADDR = bech(0x61, "44" * 28)  # enterprise key address
ORDER_ADDR = bech(0x11, ORDER_SCRIPT)  # script payment + stake


def io(h: str, idx: int, addr: str, stake: str | None, ada: float, assets: dict[str, int] | None = None,
       datum: bool = False) -> dict[str, Any]:
    return {"tx_hash": h, "tx_index": idx, "payment_addr": {"bech32": addr}, "stake_addr": stake,
            "value": str(int(ada * 1_000_000)),
            "asset_list": [{"policy_id": k.split(".")[0], "asset_name": k.split(".")[1], "quantity": str(q),
                            "decimals": 0} for k, q in (assets or {}).items()],
            "datum_hash": "dd" * 32 if datum else None, "inline_datum": None}


def tx(h: str, ts: int, inputs: list[dict[str, Any]], outputs: list[dict[str, Any]], fee: float = 0.2,
       msg: str = "", **kw: Any) -> dict[str, Any]:
    return {"tx_hash": h, "block_height": ts, "tx_timestamp": ts, "fee": str(int(fee * 1_000_000)),
            "inputs": inputs, "outputs": outputs, "metadata": {"674": {"msg": [msg]}} if msg else {},
            "withdrawals": kw.get("withdrawals", []), "certificates": kw.get("certs", []),
            "assets_minted": [], "deposit": str(kw.get("deposit", 0))}


def run(txs: list[dict[str, Any]]) -> tuple[list[Classified], CardanoState]:
    bundle = CardanoRawBundle(stake=S, txs={t["tx_hash"]: t for t in txs})
    moves, ctxs = normalize(CHAIN, bundle, {S})
    discovered: dict[str, tuple[bool | None, str]] = {ORDER_ADDR: (True, "")}
    ctx = RuleContext(chain=CHAIN, wallets={S}, registry=Registry(discovered=discovered))
    state = CardanoState(scripts={ORDER_SCRIPT: ScriptInfo("minswap-v2", "order")})
    rows = classify_cardano_wallet(S, moves, ctxs, ctx, state)
    finalize_orders(state, lambda refs: {r: False for r in refs})
    return rows, state


def test_address_header_parsing() -> None:
    a, sc = parse_address(WALLET_ADDR), parse_address(ORDER_ADDR)
    assert a and sc and a.is_key_payment and sc.is_script_payment and sc.payment_cred == ORDER_SCRIPT
    assert parse_address("Ae2tdPwUPEZ") is None


def test_simple_send_reconciles_and_change_is_not_a_transfer() -> None:
    t = tx("t1", 1, [io("p", 0, WALLET_ADDR, S, 100)],
           [io("t1", 0, EOA_ADDR, OTHER_STAKE, 30), io("t1", 1, WALLET_ADDR, S, 69.8)])
    rows, _ = run([t])
    [r] = rows
    assert (r.classification, r.movement.amount, r.gas_fee_native) == (Cls.TRANSFER, Decimal(-30), Decimal("0.2"))


def test_script_output_with_our_stake_key_is_not_wallet_funds() -> None:
    # order placed at a script address that carries the wallet's own stake key (Minswap V2 style)
    order_with_our_stake = bech(0x11, ORDER_SCRIPT, "22" * 28)
    t = tx("t2", 1, [io("p", 0, WALLET_ADDR, S, 100)],
           [io("t2", 0, order_with_our_stake, S, 50, datum=True), io("t2", 1, WALLET_ADDR, S, 49.8)])
    bundle = CardanoRawBundle(stake=S, txs={"t2": t})
    moves, _ = normalize(CHAIN, bundle, {S})
    assert [(m.amount, m.utxo_ref) for m in moves] == [(Decimal(-50), "t2#0")]


def test_min_ada_travels_with_tokens() -> None:
    t = tx("t3", 1, [io("p", 0, WALLET_ADDR, S, 10, {SNEK: 1000})],
           [io("t3", 0, EOA_ADDR, OTHER_STAKE, 1.2, {SNEK: 1000}), io("t3", 1, WALLET_ADDR, S, 8.6)])
    rows, _ = run([t])
    assert {(r.movement.symbol, r.movement.source, r.leg_role) for r in rows} == {
        ("SNEK", "utxo", "out"), ("ADA", "min_ada", "min_ada")}


def _place(h: str = "o1", ts: int = 1, msg: str = "Minswap: Limit Order") -> dict[str, Any]:
    return tx(h, ts, [io("p", 0, WALLET_ADDR, S, 200)],
              [io(h, 0, ORDER_ADDR, None, 102, datum=True), io(h, 1, WALLET_ADDR, S, 97.8)], msg=msg)


def test_order_placed_then_filled_by_batcher_is_linked_exactly() -> None:
    fill = tx("f1", 2, [io("o1", 0, ORDER_ADDR, None, 102), io("b", 0, BATCHER_ADDR, None, 5)],
              [io("f1", 0, WALLET_ADDR, S, 2, {SNEK: 5000}), io("f1", 1, BATCHER_ADDR, None, 104.5)],
              msg="Minswap: Order Executed")
    rows, _ = run([_place(), fill])
    placed = [r for r in rows if r.tx.tx_hash == "o1"]
    filled = [r for r in rows if r.tx.tx_hash == "f1"]
    assert {(r.classification, r.subtype, r.linked_tx_hash) for r in placed} == {(Cls.ORDER_PLACED, "limit:filled", "f1")}
    assert {(r.classification, r.linked_tx_hash) for r in filled} == {(Cls.ORDER_FILLED, "o1")}
    assert placed[0].protocol == "minswap-v2" and placed[0].confidence == 1.0
    assert all(r.gas_fee_native is None for r in filled)  # batcher paid


def test_wallet_signed_cancel_and_batcher_refund() -> None:
    cancel = tx("c1", 2, [io("o1", 0, ORDER_ADDR, None, 102), io("p2", 0, WALLET_ADDR, S, 5)],
                [io("c1", 0, WALLET_ADDR, S, 106.7)])
    rows, _ = run([_place(), cancel])
    assert {(r.classification, r.subtype) for r in rows if r.tx.tx_hash == "c1"} == {(Cls.ORDER_CANCELLED, "cancel")}
    assert {r.subtype for r in rows if r.tx.tx_hash == "o1"} == {"limit:cancelled"}

    refund = tx("r1", 2, [io("o1", 0, ORDER_ADDR, None, 102), io("b", 0, BATCHER_ADDR, None, 5)],
                [io("r1", 0, WALLET_ADDR, S, 101.5), io("r1", 1, BATCHER_ADDR, None, 5.3)])
    rows, _ = run([_place(), refund])
    assert {(r.classification, r.subtype) for r in rows if r.tx.tx_hash == "r1"} == {(Cls.ORDER_CANCELLED, "refund")}


def test_partial_fill_chain_through_tx_that_pays_nothing() -> None:
    # p1: partial match consumes our order, re-creates it at the same script, pays us nothing
    p1 = tx("m1", 2, [io("o1", 0, ORDER_ADDR, None, 102), io("x", 0, BATCHER_ADDR, None, 50)],
            [io("m1", 0, ORDER_ADDR, None, 60, {SNEK: 2000}, datum=True), io("m1", 1, BATCHER_ADDR, None, 91.8)])
    final = tx("m2", 3, [io("m1", 0, ORDER_ADDR, None, 60, {SNEK: 2000}), io("y", 0, BATCHER_ADDR, None, 50)],
               [io("m2", 0, WALLET_ADDR, S, 2, {SNEK: 5000}), io("m2", 1, BATCHER_ADDR, None, 107.8)])
    rows, _ = run([_place(), p1, final])
    assert {r.subtype for r in rows if r.tx.tx_hash == "o1"} == {"limit:partial→filled"}
    assert {(r.classification, r.linked_tx_hash) for r in rows if r.tx.tx_hash == "m2"} == {(Cls.ORDER_FILLED, "o1")}


def test_chase_finds_intermediate_partial_match_from_cache(tmp_path: Any) -> None:
    store = Store(tmp_path / "c.sqlite")
    p1 = tx("m1", 2, [io("o1", 0, ORDER_ADDR, None, 102)], [io("m1", 0, ORDER_ADDR, None, 60, {SNEK: 2000}, datum=True)])
    store.put_raw("koios", "tx_info", {"tx": "m1"}, p1)  # only reachable via the chase, never via HTTP
    final = tx("m2", 3, [io("m1", 0, ORDER_ADDR, None, 60, {SNEK: 2000})], [io("m2", 0, WALLET_ADDR, S, 2, {SNEK: 5000})])
    fetcher = CardanoFetcher(CHAIN, store)
    found = fetcher.chase_order_chains(S, {"o1": _place(), "m2": final})
    assert set(found) == {"m1"}


def test_staking_withdrawal_and_registration_reconcile() -> None:
    t = tx("s1", 1, [io("p", 0, WALLET_ADDR, S, 100)], [io("s1", 0, WALLET_ADDR, S, 140.8)],
           withdrawals=[{"stake_addr": S, "amount": "43000000"}], deposit=2_000_000,
           certs=[{"type": "stake_registration"}, {"type": "delegation"}])
    rows, _ = run([t])
    assert {(r.subtype, r.movement.amount) for r in rows} == {
        ("staking_reward_withdrawal", Decimal(43)), ("stake_registration", Decimal(-2))}
    assert sum((r.movement.amount for r in rows), Decimal(0)) - Decimal("0.2") == Decimal("40.8")


def test_script_release_passed_to_own_wallet_is_passthrough_not_transfer() -> None:
    night = "aa" * 28 + ".4e49474854"
    other_wallet = bech(0x01, "55" * 28)
    t = tx("n1", 1, [io("p", 0, WALLET_ADDR, S, 5), io("lock", 0, ORDER_ADDR, None, 1.2, {night: 100})],
           [io("n1", 0, other_wallet, OTHER_STAKE, 1.2, {night: 100}), io("n1", 1, WALLET_ADDR, S, 4.8)])
    rows, _ = run([t])
    assert {(r.classification, r.subtype) for r in rows} == {(Cls.OTHER, "self_or_passthrough")}


def test_lp_zap_detected_by_mint_and_burn_in_settlement() -> None:
    lq = "cc" * 28 + ".534e454b5f4144415f4c51"  # SNEK_ADA_LQ
    place = tx("z1", 1, [io("p", 0, WALLET_ADDR, S, 10, {SNEK: 1000})],
               [io("z1", 0, ORDER_ADDR, None, 4, {SNEK: 1000}, datum=True), io("z1", 1, WALLET_ADDR, S, 5.8)])
    settle = tx("z2", 2, [io("z1", 0, ORDER_ADDR, None, 4, {SNEK: 1000}), io("b", 0, BATCHER_ADDR, None, 9)],
                [io("z2", 0, WALLET_ADDR, S, 2, {lq: 777}), io("z2", 1, BATCHER_ADDR, None, 10.8)])
    settle["assets_minted"] = [{"policy_id": "cc" * 28, "asset_name": "534e454b5f4144415f4c51", "quantity": "777",
                                "decimals": 0}]
    rows, _ = run([place, settle])
    assert {r.classification for r in rows if r.tx.tx_hash == "z2"} == {Cls.LP_DEPOSIT}
    assert {r.subtype for r in rows if r.tx.tx_hash == "z1"} == {"unknown:lp_minted"}


def test_epoch_rewards_booked_in_reward_account_and_withdrawal_is_internal() -> None:
    from loledger.adapters.cardano.normalize import epoch_start, reward_account, reward_account_movements
    from loledger.decode.cardano_orders import classify_reward_account

    t = tx("w1", epoch_start(560) + 100, [io("p", 0, WALLET_ADDR, S, 100)], [io("w1", 0, WALLET_ADDR, S, 109.8)],
           withdrawals=[{"stake_addr": S, "amount": "10000000"}])
    bundle = CardanoRawBundle(stake=S, txs={"w1": t})
    moves, ctxs = normalize(CHAIN, bundle, {S})
    hist = [{"earned_epoch": e, "spendable_epoch": e + 2, "amount": str(a), "type": "member", "pool_id_bech32": "pool1x"}
            for e, a in ((555, 4_000_000), (556, 6_500_000), (557, 700_000))]
    rmoves, rtxs = reward_account_movements(CHAIN, S, hist, ctxs, epoch_now=558)  # epoch 557's reward not credited yet
    assert [m.timestamp for m in rmoves if m.amount > 0] == [epoch_start(557), epoch_start(558)]
    assert sum((m.amount for m in rmoves), Decimal(0)) == Decimal("0.5")  # = live rewards_available
    rrows = classify_reward_account(S, rmoves, {**ctxs, **rtxs})
    assert {(r.classification, r.subtype, r.movement.amount) for r in rrows} == {
        (Cls.OTHER, "staking_reward", Decimal(4)), (Cls.OTHER, "staking_reward", Decimal("6.5")),
        (Cls.INTERNAL_TRANSFER, "reward_withdrawal", Decimal(-10))}
    assert all(r.movement.wallet == reward_account(S) for r in rrows)
    ctx = RuleContext(chain=CHAIN, wallets={S}, registry=Registry())
    state = CardanoState(scripts={}, reward_accounts={S})
    rows = classify_cardano_wallet(S, moves, ctxs, ctx, state)
    assert [(r.classification, r.subtype, r.movement.amount) for r in rows] == [
        (Cls.INTERNAL_TRANSFER, "reward_withdrawal", Decimal(10))]


def test_wallet_signed_relock_keeps_the_order_open() -> None:
    # a token order re-locked by the wallet at the same script with the same tokens is an update, not a cancel
    place = tx("o1", 1, [io("p1", 0, WALLET_ADDR, S, 10, {SNEK: 500})],
               [io("o1", 0, ORDER_ADDR, None, 1.5, {SNEK: 500}, datum=True), io("o1", 1, WALLET_ADDR, S, 8.3)])
    relock = tx("u1", 2, [io("o1", 0, ORDER_ADDR, None, 1.5, {SNEK: 500}), io("o1", 1, WALLET_ADDR, S, 8.3)],
                [io("u1", 0, ORDER_ADDR, None, 1.5, {SNEK: 500}, datum=True), io("u1", 1, WALLET_ADDR, S, 5.6),
                 io("u1", 2, BATCHER_ADDR, None, 2.5)])
    cancel = tx("c1", 3, [io("u1", 0, ORDER_ADDR, None, 1.5, {SNEK: 500}), io("u1", 1, WALLET_ADDR, S, 5.6)],
                [io("c1", 0, WALLET_ADDR, S, 6.9, {SNEK: 500})])
    rows, _ = run([place, relock, cancel])
    assert {(r.classification, r.subtype, r.leg_role) for r in rows if r.tx.tx_hash == "u1" and r.movement.source != "gas"} \
        == {(Cls.OTHER, "order_updated", "fee")}
    assert {(r.classification, r.linked_tx_hash) for r in rows if r.tx.tx_hash == "c1"} == {(Cls.ORDER_CANCELLED, "o1")}
    assert {r.subtype for r in rows if r.tx.tx_hash == "o1"} == {"unknown:cancelled"}
