"""Hand-built scenarios: one per classification, plus tricky cases."""

from __future__ import annotations

from decimal import Decimal

from txclass.model import NATIVE_ASSET, AssetKind, Log, Movement, TxContext
from txclass.registry import ZERO, ContractInfo, Registry
from txclass.rules import Classified, Cls, RuleContext, TRANSFER_TOPIC, classify_wallet, dedupe_gas, link_same_tx_internal

from .fakes import CHAIN

W = "0x" + "a1" * 19 + "d2"
W2 = "0x" + "b2" * 20
EOA = "0x" + "e0" * 20
USDC = "0x" + "c1" * 20
LINK = "0x" + "c2" * 20
PAIR = "0x" + "aa" * 19 + "01"
ROUTER = "0x" + "dd" * 20
DSLO = "0x" + "d5" * 20
EXEC = "0x" + "e5" * 20
FEE = "0x" + "fe" * 20
GROUTER = "0x" + "90" * 20
GCORE = "0x" + "91" * 20
VAULT = "0x" + "92" * 20
KEEPER = "0x" + "99" * 20
WAVAX = CHAIN.wrapped_native or ""

REG = Registry(
    tokens={USDC: "USDC", LINK: "LINK.e", WAVAX: "WAVAX"},
    contracts={
        ROUTER: ContractInfo("Router", "router", "someswap"),
        DSLO: ContractInfo("DSLOProtocol", "limit_order_protocol", "kyberswap-limit-order"),
        EXEC: ContractInfo("executor", "settlement", "kyberswap-limit-order"),
        FEE: ContractInfo("fee", "fee_recipient", "kyberswap-limit-order"),
        GROUTER: ContractInfo("ERC20OrderRouter", "order_router", "gelato-limit-order"),
        GCORE: ContractInfo("GelatoPineCore", "order_book", "gelato-limit-order"),
        WAVAX: ContractInfo("WAVAX", "wrapper", "wavax"),
    },
    discovered={PAIR: (True, "JoePair"), EOA: (False, ""), W2: (False, ""), VAULT: (False, "")},
)

_n = 0


def tx(h: str, frm: str = W, to: str = ROUTER, *, err: bool = False, ts: int = 0, logs: list[Log] | None = None,
       method: str = "0x12345678") -> TxContext:
    return TxContext(chain="Avalanche", tx_hash=h, block=ts or 1, timestamp=ts or 1, tx_from=frm, tx_to=to,
                     method_id=method, function_name="", is_error=err, gas_fee=Decimal("0.01"), value=Decimal(0),
                     logs=logs or [])


def mv(h: str, asset: str, amount: str, cp: str, *, sym: str = "", kind: AssetKind = AssetKind.ERC20,
       source: str = "tokentx", ts: int = 1, wallet: str = W) -> Movement:
    global _n
    _n += 1
    return Movement(chain="Avalanche", tx_hash=h, leg_id=f"l{_n}", block=ts, timestamp=ts, wallet=wallet,
                    asset_id=asset, asset_kind=kind, symbol=sym or REG.tokens.get(asset, "TKN"), decimals=18,
                    amount=Decimal(amount), counterparty=cp, source=source)


def run(moves: list[Movement], txs: list[TxContext], wallet: str = W) -> list[Classified]:
    ctx = RuleContext(chain=CHAIN, wallets={W, W2}, registry=REG)
    return classify_wallet(wallet, moves, {t.tx_hash: t for t in txs}, ctx)


def kinds(rows: list[Classified]) -> set[tuple[str, str]]:
    return {(r.classification.value, r.subtype) for r in rows}


def test_external_transfer_out_and_in() -> None:
    rows = run([mv("0x1", USDC, "-5", EOA), mv("0x2", USDC, "7", EOA)],
               [tx("0x1", to=USDC), tx("0x2", frm=EOA, to=USDC)])
    assert kinds(rows) == {("TRANSFER", "to_eoa"), ("TRANSFER", "from_eoa")}
    assert rows[0].gas_fee_native == Decimal("0.01") and rows[1].gas_fee_native is None


def test_internal_transfer_with_fee_on_transfer_token() -> None:
    # 100 sent, 98 arrive (2% transfer tax burned): both sides are still INTERNAL_TRANSFER
    out = run([mv("0x3", LINK, "-100", W2)], [tx("0x3", to=LINK)])
    inn = run([mv("0x3", LINK, "98", W, wallet=W2)], [tx("0x3", to=LINK)], wallet=W2)
    assert kinds(out) == kinds(inn) == {("INTERNAL_TRANSFER", "")}


def test_multi_hop_swap_nets_out_intermediate_token() -> None:
    rows = run([mv("0x4", USDC, "-100", ROUTER), mv("0x4", WAVAX, "4", PAIR), mv("0x4", WAVAX, "-4", PAIR),
                mv("0x4", LINK, "6", ROUTER)], [tx("0x4")])
    assert kinds(rows) == {("SWAP", "market")}
    assert {r.movement.symbol: r.leg_role for r in rows} == {"USDC": "out", "WAVAX": "passthrough", "LINK.e": "in"}
    assert rows[0].protocol == "someswap"


def test_signed_limit_order_fill_with_fee_leg() -> None:
    logs = [Log(DSLO, ("0xabc",), "0x", 1)]
    rows = run([mv("0x5", USDC, "-99.9", EXEC), mv("0x5", USDC, "-0.1", FEE), mv("0x5", LINK, "6", EXEC)],
               [tx("0x5", frm=KEEPER, to=ROUTER, logs=logs)])
    assert kinds(rows) == {("ORDER_FILLED", "signed_limit_order")}
    assert [r.leg_role for r in rows] == ["out", "fee", "in"]
    assert rows[0].protocol == "kyberswap-limit-order"
    assert rows[0].gas_fee_native is None  # keeper paid gas


def test_third_party_swap_without_known_protocol_is_swap() -> None:
    rows = run([mv("0x6", USDC, "-10", PAIR), mv("0x6", LINK, "1", PAIR)], [tx("0x6", frm=KEEPER)])
    assert kinds(rows) == {("SWAP", "limit_order_fill")}


def test_escrow_order_placed_then_cancelled_is_linked() -> None:
    rows = run([mv("0x7", USDC, "-199", VAULT, ts=10), mv("0x8", USDC, "199", VAULT, ts=20)],
               [tx("0x7", to=GROUTER, ts=10), tx("0x8", to=GCORE, ts=20)])
    placed, cancel = rows
    assert (placed.classification, placed.subtype, placed.linked_tx_hash) == (Cls.ORDER_PLACED, "escrow_cancelled", "0x8")
    assert (cancel.classification, cancel.linked_tx_hash) == (Cls.ORDER_CANCELLED, "0x7")


def test_escrow_order_filled_by_executor_links_through_vault_transfer_log() -> None:
    def pad(a: str) -> str:
        return "0x" + a[2:].rjust(64, "0")

    fill_logs = [Log(USDC, (TRANSFER_TOPIC, pad(VAULT), pad(GCORE)), hex(199), 3)]
    rows = run([mv("0x9", USDC, "-199", VAULT, ts=10), mv("0xa", LINK, "12", GCORE, ts=30)],
               [tx("0x9", to=GROUTER, ts=10), tx("0xa", frm=KEEPER, to=GCORE, ts=30, logs=fill_logs)])
    assert (rows[0].subtype, rows[0].linked_tx_hash) == ("escrow_filled", "0xa")
    assert (rows[1].classification, rows[1].linked_tx_hash) == (Cls.ORDER_FILLED, "0x9")


def test_unlinked_escrow_order_stays_open() -> None:
    rows = run([mv("0xb", USDC, "-1", VAULT)], [tx("0xb", to=GROUTER)])
    assert kinds(rows) == {("ORDER_PLACED", "escrow_open_or_unknown")}


def test_failed_tx_emits_gas_only_row() -> None:
    [r] = run([], [tx("0xc", err=True)])
    assert (r.classification, r.movement.amount, r.gas_fee_native) == (Cls.FAILED, Decimal(0), Decimal("0.01"))


def test_approval_is_gas_only_other_row() -> None:
    [r] = run([], [tx("0xd", to=USDC, method="0x095ea7b3")])
    assert (r.classification, r.subtype) == (Cls.OTHER, "approval")


def test_wrap_and_unwrap() -> None:
    rows = run([mv("0xe", NATIVE_ASSET, "-2", WAVAX, kind=AssetKind.NATIVE, source="txlist"),
                mv("0xe", WAVAX, "2", WAVAX, source="wrap")], [tx("0xe", to=WAVAX)])
    assert kinds(rows) == {("OTHER", "wrap")}


def test_lp_deposit_and_withdraw_generic_pair() -> None:
    dep = run([mv("0xf", USDC, "-10", PAIR), mv("0xf", LINK, "-1", PAIR), mv("0xf", PAIR, "3", ZERO)],
              [tx("0xf", to=ROUTER)])
    wd = run([mv("0x10", PAIR, "-3", PAIR), mv("0x10", USDC, "10.1", PAIR), mv("0x10", LINK, "1.01", PAIR)],
             [tx("0x10", to=ROUTER)])
    assert {r.classification for r in dep} == {Cls.LP_DEPOSIT}
    assert {r.classification for r in wd} == {Cls.LP_WITHDRAW}
    assert dep[0].confidence == 0.9  # generic pattern, not registry-confirmed


def test_spam_poisoning_fake_token_and_airdrop() -> None:
    lookalike = W[:6] + "00" * 16 + W[-4:]
    rows = run([
        mv("0x11", USDC, "0", lookalike),                        # zero-value poisoning
        mv("0x12", "0x" + "f1" * 20, "5194", lookalike, sym="USDC"),  # fake USDC from lookalike
        mv("0x13", "0x" + "f2" * 20, "-10388", EOA, sym="UЅDС"),    # homoglyph spoofed outflow
        mv("0x14", "0x" + "f3" * 20, "70000", "0x" + "f4" * 20, sym="Visit https://x.io"),
        mv("0x15", "0x" + "f5" * 20, "6.6e7", PAIR, sym="MONKE"),  # unsolicited meme token
    ], [tx(h, frm=KEEPER, to="0x" + "77" * 20) for h in ("0x11", "0x12", "0x13", "0x14", "0x15")])
    assert [r.subtype for r in rows] == ["spam_address_poisoning", "spam_address_poisoning", "spam_fake_token",
                                         "spam_link_token", "airdrop"]
    assert all(r.confidence < 1 for r in rows)


def test_wallet_sending_to_lookalike_is_a_real_transfer_with_warning() -> None:
    lookalike = W2[:6] + "00" * 16 + W2[-4:]
    [r] = run([mv("0x16", USDC, "-50", lookalike)], [tx("0x16", to=USDC)])
    assert r.classification == Cls.TRANSFER and "WARNING" in r.reason


def test_lookalike_with_three_plus_four_character_match_is_poisoning() -> None:
    w = "0x482" + "a" * 33 + "dd22"
    fake = "0x4820ac18503e1a84723b1111ab30f5f00b7add22"  # 3 + 4
    ctx = RuleContext(chain=CHAIN, wallets={w}, registry=REG)
    rows = classify_wallet(w, [mv("0x20", USDC, "0.00001", fake, wallet=w)],
                           {"0x20": tx("0x20", frm=fake, to=USDC)}, ctx)
    assert rows[0].subtype == "spam_address_poisoning"


def test_burn_to_zero_by_wallet_is_bridge_out() -> None:
    [r] = run([mv("0x21", USDC, "-1717", ZERO)], [tx("0x21", to=USDC)])
    assert (r.classification, r.subtype) == (Cls.OTHER, "bridge_out")


def test_poisoning_that_mimics_a_previous_recipient() -> None:
    friend = "0xb78c80e09ebc12b61997563eb555475266b336df"
    fake = "0xb78e285b37e09d418d7165b237dc3397b2fa36df"  # 3 + 4 lookalike of friend
    rows = run([mv("0x30", USDC, "-100", friend, ts=10), mv("0x31", "0x" + "f9" * 20, "100", fake, sym="USDC", ts=20),
                mv("0x32", USDC, "-5", fake, ts=30)],
               [tx("0x30", to=USDC, ts=10), tx("0x31", frm=fake, to="0x" + "f9" * 20, ts=20), tx("0x32", to=USDC, ts=30)])
    by = {r.tx.tx_hash: r for r in rows}
    assert by["0x31"].subtype == "spam_address_poisoning" and "sent to" in by["0x31"].reason
    # the wallet later really sends to the lookalike: a real transfer, flagged, and not trusted
    assert by["0x32"].classification == Cls.TRANSFER and "WARNING" in by["0x32"].reason


def test_native_dust_threshold_is_per_chain() -> None:
    import dataclasses
    sol = dataclasses.replace(CHAIN, name="Solana", dust_native="0.00001")
    ctx = RuleContext(chain=sol, wallets={W}, registry=REG)
    tiny = Movement(chain="Solana", tx_hash="d1", leg_id="sol:native", block=1, timestamp=1, wallet=W,
                    asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol="SOL", decimals=9,
                    amount=Decimal("0.0000001"), counterparty="spammer", source="solana")
    real = Movement(**{**tiny.__dict__, "tx_hash": "d2", "amount": Decimal("0.0001")}) if hasattr(tiny, "__dict__") else \
        dataclasses.replace(tiny, tx_hash="d2", amount=Decimal("0.0001"))
    rows = classify_wallet(W, [tiny, real], {"d1": tx("d1", frm="spammer", to="x"), "d2": tx("d2", frm="spammer", to="x")}, ctx)
    assert [r.subtype for r in rows] == ["spam_dust", "from_eoa"]


def test_phishing_note_flags_unsolicited_inflow() -> None:
    import dataclasses
    algo = dataclasses.replace(CHAIN, name="Algorand", dust_native="0")
    ctx = RuleContext(chain=algo, wallets={W}, registry=REG)
    m = Movement(chain="Algorand", tx_hash="a1", leg_id="algo:native", block=1, timestamp=1, wallet=W,
                 asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol="ALGO", decimals=6, amount=Decimal("0.1"),
                 counterparty="RULDAS", source="algorand")
    t = tx("a1", frm="RULDAS", to=W)
    t.meta = {"note": "Claim reward from Algorand governance. https://algorand-rewards.org"}
    [r] = classify_wallet(W, [m], {"a1": t}, ctx)
    assert r.subtype == "spam_phishing"


def test_mev_builder_payment_to_fee_recipient_is_proposer_reward() -> None:
    builder = "0x" + "b0" * 20
    reg = Registry(contracts={builder: ContractInfo("beaverbuild", "mev_builder", "mev-boost")})
    ctx = RuleContext(chain=CHAIN, wallets={W}, registry=reg)
    t = tx("0xm", frm=builder, to=W, method="0x")
    rows = classify_wallet(W, [mv("0xm", NATIVE_ASSET, "0.04", builder, kind=AssetKind.NATIVE, source="txlist")],
                           {"0xm": t}, ctx)
    assert kinds(rows) == {("OTHER", "proposer_reward")}


def test_fee_is_kept_once_when_a_tx_is_classified_in_several_groups() -> None:
    t = tx("0xg")
    a = Classified(mv("0xg", NATIVE_ASSET, "1", EOA, kind=AssetKind.NATIVE), t, Cls.INTERNAL_TRANSFER, "reward_withdrawal",
                   "", 1.0, "", gas_fee_native=Decimal("0.2"))
    b = Classified(mv("0xg", NATIVE_ASSET, "-5", W2, kind=AssetKind.NATIVE), t, Cls.INTERNAL_TRANSFER, "", "", 1.0, "",
                   gas_fee_native=Decimal("0.2"))
    assert dedupe_gas([a, b]) == 1
    assert (a.gas_fee_native, b.gas_fee_native) == (Decimal("0.2"), None)


def test_outflow_received_by_another_listed_wallet_in_the_same_tx_is_internal() -> None:
    t = tx("0xs", frm=W)
    ata = "0x" + "ab" * 20  # recipient's token account, not its owner
    out = Classified(mv("0xs", USDC, "-5", ata), t, Cls.TRANSFER, "to_eoa", "", 1.0, "")
    rent = Classified(mv("0xs", NATIVE_ASSET, "-0.002", ata, kind=AssetKind.NATIVE), t, Cls.TRANSFER, "to_eoa", "", 1.0, "")
    got = Classified(mv("0xs", USDC, "5", W, wallet=W2), t, Cls.INTERNAL_TRANSFER, "", "", 1.0, "")
    assert link_same_tx_internal([out, rent, got]) == 1
    assert (out.classification, rent.subtype) == (Cls.INTERNAL_TRANSFER, "account_rent")


def test_stake_withdraw_to_an_unlisted_authority_is_an_outgoing_transfer() -> None:
    stake_prog = "Stake11111111111111111111111111111111111111"
    reg = Registry(tokens={}, contracts={stake_prog: ContractInfo("Stake program", "staking", "solana-staking")},
                   discovered={EOA: (False, "")})
    t = tx("sw", frm=W2, to=stake_prog)
    t.meta["stake_ix"] = [{"type": "withdraw", "stake": W, "destination": EOA}]
    ctx = RuleContext(chain=CHAIN, wallets={W, W2}, registry=reg)
    rows = classify_wallet(W, [mv("sw", NATIVE_ASSET, "-2", EOA, kind=AssetKind.NATIVE, source="txlist")], {"sw": t}, ctx)
    assert kinds(rows) == {("TRANSFER", "to_eoa")}
    t.meta["stake_ix"] = [{"type": "delegate", "stake": W, "destination": ""}]
    rows = classify_wallet(W, [mv("sw", NATIVE_ASSET, "-2", EOA, kind=AssetKind.NATIVE, source="txlist")], {"sw": t}, ctx)
    assert kinds(rows) == {("OTHER", "stake")}
