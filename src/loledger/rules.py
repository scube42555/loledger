"""Deterministic classification of one (wallet, tx) group of movements.

Order of evaluation:
  1. failed tx (gas only)            -> FAILED
  2. Avalanche atomic C<->X/P        -> OTHER/atomic_import|atomic_export
  3. spam legs (poisoning, spoofed or link tokens, NFT spam) are split off -> OTHER/spam_*
  4. remaining legs are netted per asset and the group gets one class:
       wrap/unwrap, yield vault, LP mint/burn, SWAP (market or third-party-executed fill),
       ORDER_PLACED (registry order_book/escrow), INTERNAL_TRANSFER, TRANSFER, bridge, airdrop, claim
  5. a wallet-sent tx with no remaining legs (approve, cancel, ...) -> OTHER/contract_call row for gas
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .config import ChainConfig
from .model import NATIVE_ASSET, AssetKind, Movement, TxContext
from .registry import ZERO, Registry, norm_symbol

CHIA_BUNDLE_CP = "chia:other_inputs_in_same_spend"  # = adapters.chia.normalize.BUNDLE_CP
URLISH = re.compile(r"https?|www\.|\.(com|io|org|net|xyz|app|gift|site|finance|top|cc|me)\b|t\.me|visit|claim|reward",
                    re.IGNORECASE)
FEE_SHARE = Decimal("0.01")  # an extra outflow of a sold asset <= 1% of the total is a fee leg
APPROVE_IDS = {"0x095ea7b3": "approval", "0x39509351": "approval", "0xa22cb465": "approval"}


class Cls(StrEnum):
    TRANSFER = "TRANSFER"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    SWAP = "SWAP"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    LP_DEPOSIT = "LP_DEPOSIT"
    LP_WITHDRAW = "LP_WITHDRAW"
    LP_FEES_CLAIMED = "LP_FEES_CLAIMED"
    FAILED = "FAILED"
    OTHER = "OTHER"


@dataclass(slots=True)
class Classified:
    movement: Movement
    tx: TxContext
    classification: Cls
    subtype: str
    protocol: str
    confidence: float
    reason: str
    leg_role: str = ""
    linked_tx_hash: str = ""
    gas_fee_native: Decimal | None = None
    datum_summary: str = ""
    order_id: str = ""
    order_details: str = ""
    usd_price: Decimal | None = None
    usd_value: Decimal | None = None
    price_source: str = ""
    gas_fee_usd: Decimal | None = None


@dataclass
class RuleContext:
    chain: ChainConfig
    wallets: set[str]
    registry: Registry
    unknown_contracts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # per wallet: addresses it has genuinely sent to (chronological, lookalikes excluded)
    recipients: dict[str, set[str]] = field(default_factory=dict)

    def trusted(self, wallet: str) -> set[str]:
        return self.wallets | self.recipients.get(wallet, set())


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


# fixed leading characters shared by every address of a format (not evidence of imitation)
_FORMAT_PREFIXES = ("kaspa:q", "kaspa:p", "bc1q", "bc1p", "stake1u", "addr1q", "addr1v", "addr1z", "addr1w", "addr1", "0x")
_BASE58_VERSIONS = ("D", "L", "M", "1", "3")
_PSEUDO = ("cardano:", "avalanche:", "eth2:", "spam")


def _addr_like(a: str) -> bool:
    return bool(a) and not a.startswith(_PSEUDO) and a != ZERO and len(a) >= 26


def _strip_format(a: str) -> tuple[str, str]:
    for p in _FORMAT_PREFIXES:
        if a.startswith(p):
            return p, a[len(p):]
    if a[:1] in "DLM13" and len(a) in range(26, 36):  # base58 (Doge/LTC/legacy BTC): version char
        return a[:1], a[1:]
    return "", a


def _lookalike(a: str, wallets: set[str], *, prefix_only: bool = False) -> str | None:
    """Address-poisoning lookalike of one of our wallets.

    Poisoners grind vanity addresses matching the start and end that wallets display. Seen in this
    data: 4+4 and 3+4 / 4+3 character matches. Require >=3 on each side and >=7 in total; a random
    address hits that with probability ~16^-7 per wallet.
    """
    if a in wallets or not _addr_like(a):
        return None
    fa, ba = _strip_format(a)
    for w in wallets:
        fw, bw = _strip_format(w)
        if fw != fa or len(bw) != len(ba) or not _addr_like(w):
            continue
        pre = _common_prefix(ba, bw)
        suf = _common_prefix(ba[::-1], bw[::-1])
        if pre >= 3 and suf >= 3 and pre + suf >= 7:
            return w
        # Doge poisoners grind only the start shown in wallets (first 5 chars incl. the 'D'); 4 base58
        # chars match by chance ~1 in 11M. Only used for unsolicited inflows, never for the user's sends.
        if prefix_only and fa in _BASE58_VERSIONS and pre >= 4:
            return w
    return None


def is_governance_reward(tx: TxContext) -> bool:
    """Algorand Foundation governance payouts carry an 'af/gov1:j{...rewardsPrd...}' note."""
    note = str(tx.meta.get("note") or "")
    return note.startswith("af/gov1:") and "rewardsPrd" in note


def spam_reason(m: Movement, tx: TxContext, ctx: RuleContext) -> tuple[str, str] | None:
    """(subtype, reason) if this leg is spam. Never flags legs of a tx the wallet itself sent."""
    if tx.tx_from == m.wallet:
        return None
    reg = ctx.registry
    if m.asset_kind in (AssetKind.ERC721, AssetKind.ERC1155) and m.amount > 0:
        return "spam_nft", "unsolicited NFT/1155 received in a tx the wallet did not send"
    if m.amount == 0:
        return "spam_address_poisoning", "zero-value transfer in a tx the wallet did not send (address poisoning)"
    note = str(tx.meta.get("note") or "")
    if is_governance_reward(tx):
        return None
    if note and m.amount > 0 and URLISH.search(note):
        return "spam_phishing", f"unsolicited transfer with a lure in its note: {note[:80]!r}"
    look = _lookalike(m.counterparty, ctx.trusted(m.wallet), prefix_only=m.amount > 0)
    if look:
        whose = "own wallet" if look in ctx.wallets else "an address this wallet sent to"
        fake_tok = (m.asset_kind == AssetKind.ERC20 and m.asset_id not in reg.tokens
                    and norm_symbol(m.symbol) in reg.canonical_symbols)
        return "spam_address_poisoning", (f"counterparty {m.counterparty[:8]}…{m.counterparty[-4:]} mimics {whose} "
                                          f"{look[:8]}…{look[-4:]}" + ("; the token is a fake copy of "
                                          f"{m.symbol} (not the real contract), so nothing actually left the wallet"
                                          if fake_tok else ""))
    dust = Decimal(ctx.chain.dust_native or "0")
    if m.asset_kind == AssetKind.NATIVE and dust > 0 and 0 < m.amount <= dust:
        return "spam_dust", f"unsolicited dust ({m.amount.normalize():f} {m.symbol} ≤ {dust} threshold)"
    if m.asset_kind == AssetKind.ERC20:
        if m.asset_id not in reg.tokens:
            if any(ord(c) > 127 for c in m.symbol):
                return "spam_fake_token", f"non-ASCII lookalike symbol {m.symbol!r}"
            if URLISH.search(m.symbol):
                return "spam_link_token", f"symbol contains a link/lure: {m.symbol!r}"
            if norm_symbol(m.symbol) in reg.canonical_symbols:
                return "spam_fake_token", f"symbol {m.symbol!r} imitates a canonical token at a different address"
    c = reg.contract(tx.tx_to)
    if c and c.kind == "spam_distributor":
        return "spam_airdrop", f"sent by known spam distributor {c.name}"
    return None


def _lop_in_logs(tx: TxContext, reg: Registry) -> str:
    """Protocol name of a registered limit-order protocol that emitted events in this tx, else ''."""
    for lg in tx.logs:
        c = reg.contract(lg.address)
        if c and c.kind == "limit_order_protocol":
            return c.protocol
    return ""


def _protocol(tx: TxContext, legs: list[Movement], reg: Registry) -> str:
    lop = _lop_in_logs(tx, reg)
    if lop:
        return lop
    for a in [tx.tx_to, *[m.counterparty for m in legs]]:
        c = reg.contract(a)
        if c and c.kind not in ("wrapper", "fee_recipient"):
            return c.protocol
    for a in [tx.tx_to, *[m.counterparty for m in legs]]:
        name = reg.label(a)
        if name and a not in reg.tokens:
            return name
    return ""


def _net(legs: list[Movement]) -> dict[str, Decimal]:
    """Net per asset. Cardano min-ADA legs ride along with tokens and do not drive the class."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for m in legs:
        if m.source == "min_ada":
            continue
        out[m.asset_id] += m.amount
    return {a: v for a, v in out.items() if v != 0}


def _gas_row(wallet: str, tx: TxContext, chain: ChainConfig) -> Movement:
    return Movement(chain=tx.chain, tx_hash=tx.tx_hash, leg_id="gas", block=tx.block, timestamp=tx.timestamp,
                    wallet=wallet, asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol,
                    decimals=chain.native_decimals, amount=Decimal(0), counterparty=tx.tx_to, source="gas")


def classify_group(wallet: str, tx: TxContext, legs: list[Movement], ctx: RuleContext) -> list[Classified]:
    reg, chain = ctx.registry, ctx.chain
    sent = tx.tx_from == wallet
    out: list[Classified] = []

    def emit(m: Movement, cls: Cls, subtype: str, reason: str, *, protocol: str = "", conf: float = 1.0,
             role: str = "") -> None:
        out.append(Classified(m, tx, cls, subtype, protocol, conf, reason, role))

    if tx.is_error:
        if sent:
            emit(_gas_row(wallet, tx, chain), Cls.FAILED, tx.function_name.split("(")[0] or tx.method_id,
                 "tx reverted (isError=1): gas paid, no token movement", protocol=_protocol(tx, [], reg))
        return _with_gas(out, wallet, tx)

    if tx.tx_hash.startswith("atomic:"):
        for m in legs:
            emit(m, Cls.OTHER, tx.function_name, "C-Chain balance change with no EVM tx: Avalanche atomic "
                 "import/export to X/P-Chain (found by balance reconciliation)", protocol="avalanche-atomic")
        return out

    if tx.tx_hash.startswith("block:"):
        for m in legs:
            emit(m, Cls.OTHER, "proposer_reward", f"wallet was fee recipient of block {tx.block} "
                 "(validator proposer reward: priority fees / MEV)", protocol="eth2-staking")
        return out

    if tx.tx_hash.startswith("beacon:"):
        vals = ",".join(str(v) for v in dict.fromkeys(tx.meta.get("validators", [])))
        for m in legs:
            sub = "beacon_exit" if m.amount >= 16 else "beacon_reward"  # principal back (≥16 ETH) vs reward sweep
            emit(m, Cls.OTHER, sub, f"consensus-layer withdrawal from validator {vals} "
                 f"({'principal returned on exit' if sub == 'beacon_exit' else 'partial withdrawal of rewards'})",
                 protocol="eth2-staking")
        return out

    builder = reg.contract(tx.tx_from)
    if not sent and builder and builder.kind == "mev_builder" and legs and all(
            m.amount > 0 and m.asset_kind == AssetKind.NATIVE for m in legs):
        for m in legs:
            emit(m, Cls.OTHER, "proposer_reward", f"MEV-boost payment from block builder {builder.name} to this fee "
                 f"recipient for block {tx.block} (validator proposer reward)", protocol="eth2-staking")
        return out

    rewards = [m for m in legs if m.amount > 0 and (m.source in ("reward_withdrawal", "farming_reward", "staking_reward")
                                                     or is_governance_reward(tx))]
    if rewards:  # staking rewards auto-withdrawn alongside delegate/undelegate/redelegate; farming rewards
        for m in rewards:
            if is_governance_reward(tx):
                emit(m, Cls.OTHER, "governance_reward", "Algorand Foundation governance reward payout (af/gov note)",
                     protocol="algorand-governance", role="in")
            elif m.source == "staking_reward":
                why = (f"epoch {tx.meta['epoch']} inflation reward credited to this stake account (no transaction; "
                       "income when earned)") if "epoch" in tx.meta else \
                    f"validator-shared reward paid to this stake account by {m.counterparty[:10]}… (MEV tips / block rewards)"
                emit(m, Cls.OTHER, "staking_reward", why, protocol="solana-staking", role="in")
            elif m.source == "farming_reward":
                emit(m, Cls.OTHER, "farming_reward", "block reward paid to this farmer/pool address (coinbase coin)",
                     protocol="chia-farming", role="in")
            else:
                emit(m, Cls.OTHER, "staking_reward_withdrawal", "staking rewards paid by the distribution module",
                     protocol=_protocol(tx, [], reg) or "staking", role="in")
        legs = [m for m in legs if m not in rewards]
        if not legs:
            return _with_gas(out, wallet, tx)

    if chain.kind == "chia":  # coin spends: legs are independent payments, classify each on its own
        rest: list[Movement] = []
        for m in legs:
            if m.counterparty in ctx.wallets:
                emit(m, Cls.INTERNAL_TRANSFER, "", "counterparty is another wallet in the list"
                     + (" (coins pooled into one wallet spend)" if m.leg_id.startswith("bundle") else ""),
                     role="out" if m.amount < 0 else "in")
            elif m.counterparty == CHIA_BUNDLE_CP:
                emit(m, Cls.INTERNAL_TRANSFER, "pooled_wallet_coins",
                     "coins of this wallet's other (unlisted) addresses spent together with this address's coins "
                     f"({'they funded part of this spend' if m.amount > 0 else 'this address funded part of it'})",
                     conf=0.9, role="out" if m.amount < 0 else "in")
            else:
                rest.append(m)
        if not rest:
            return _with_gas(out, wallet, tx)
        legs = rest

    real: list[Movement] = []
    for m in legs:
        sr = spam_reason(m, tx, ctx)
        if sr:
            emit(m, Cls.OTHER, sr[0], sr[1], protocol="spam", conf=0.95)
        else:
            real.append(m)

    if not real:
        if sent and not out:
            fn = tx.function_name.split("(")[0] or tx.method_id
            if fn == "consolidation":
                emit(_gas_row(wallet, tx, chain), Cls.OTHER, "consolidation",
                     "wallet spent its own coins back to itself (consolidation / change only), fee only",
                     protocol=_protocol(tx, [], reg))
                return _with_gas(out, wallet, tx)
            if fn in ("governance_vote", "consensus_keyreg"):
                emit(_gas_row(wallet, tx, chain), Cls.OTHER, fn,
                     {"governance_vote": f"governance vote (note {str(tx.meta.get('note', ''))[:40]!r})",
                      "consensus_keyreg": "consensus participation key registration"}[fn], protocol="algorand-governance"
                     if fn == "governance_vote" else "algorand-consensus")
                return _with_gas(out, wallet, tx)
            if fn == "fee_payer_only":
                emit(_gas_row(wallet, tx, chain), Cls.OTHER, "fee_payer_only",
                     "this account only paid the fee; the assets moved belong to another account", protocol=_protocol(tx, [], reg))
                _note_unknown(tx.tx_to, ctx)
                return _with_gas(out, wallet, tx)
            info = reg.contract(tx.tx_to)
            if info and info.kind == "staking":
                sub, why = "stake_management", f"{info.name}: delegate/deactivate/withdraw with no asset movement here"
            elif not fn and not tx.method_id:
                sub, why = "account_maintenance", "system-program only tx (account setup/maintenance), gas only"
            else:
                sub, why = APPROVE_IDS.get(tx.method_id, f"contract_call:{fn}"), "wallet-sent tx with no asset movement (gas only)"
            emit(_gas_row(wallet, tx, chain), Cls.OTHER, sub, why, protocol=_protocol(tx, [], reg))
            _note_unknown(tx.tx_to, ctx)
        return _with_gas(out, wallet, tx)

    net = _net(real)
    outs = {a for a, v in net.items() if v < 0}
    ins = {a for a, v in net.items() if v > 0}
    protocol = _protocol(tx, real, reg)
    for a in {tx.tx_to, *(m.counterparty for m in real)}:
        _note_unknown(a, ctx)
    tx_to_info = reg.contract(tx.tx_to)
    cps = {m.counterparty for m in real}

    def all_legs(cls: Cls, subtype: str, reason: str, conf: float = 1.0) -> None:
        for m in real:
            emit(m, cls, subtype, reason, protocol=protocol, conf=conf, role=_role(m, net))

    wrapped = chain.wrapped_native
    if wrapped and set(net) <= {NATIVE_ASSET, wrapped} and outs and ins and any(m.source == "wrap" for m in real):
        kind = "wrap" if NATIVE_ASSET in outs else "unwrap"
        all_legs(Cls.OTHER, kind, f"{chain.native_symbol}<->wrapped {kind} (Deposit/Withdrawal event)")
        return _with_gas(out, wallet, tx)

    vault = next((i for i in [tx_to_info, *(reg.contract(c) for c in cps)] if i and i.kind == "yield_vault"), None)
    if vault:
        if outs and not ins:
            who = "" if sent else f" (submitted by third party {tx.tx_from[:10]})"
            all_legs(Cls.OTHER, "yield_deposit", f"deposit into yield vault {vault.name}{who}")
            return _with_gas(out, wallet, tx)
        if ins and not outs:
            all_legs(Cls.OTHER, "yield_withdraw", f"withdrawal from yield vault {vault.name}")
            return _with_gas(out, wallet, tx)

    lp = _lp_kind(real, net, reg)
    if lp:
        cls, reason, conf = lp
        all_legs(cls, "lp", reason, conf)
        return _with_gas(out, wallet, tx)

    lop = _lop_in_logs(tx, reg)
    if outs and ins and lop and not sent:
        all_legs(Cls.ORDER_FILLED, "signed_limit_order",
                 f"{lop} fill event in this tx; wallet is maker, fill submitted by {tx.tx_from[:10]}: "
                 f"out {_syms(real, outs)} / in {_syms(real, ins)} (order was signed off-chain, no placement tx)")
        return _with_gas(out, wallet, tx)

    if outs and ins:
        if sent:
            all_legs(Cls.SWAP, "market", f"net out {_syms(real, outs)} / in {_syms(real, ins)} in one tx sent by the wallet")
        else:
            all_legs(Cls.SWAP, "limit_order_fill",
                     f"atomic swap of wallet funds executed by third party {tx.tx_from[:10]} (signed/limit order fill): "
                     f"out {_syms(real, outs)} / in {_syms(real, ins)}")
        return _with_gas(out, wallet, tx)

    if not outs and not ins:
        all_legs(Cls.OTHER, "self_or_passthrough", "legs net to zero for this wallet")
        return _with_gas(out, wallet, tx)

    if cps and cps <= ctx.wallets:
        all_legs(Cls.INTERNAL_TRANSFER, "", "counterparty is another wallet in the list")
        return _with_gas(out, wallet, tx)

    order_kinds = ("order_book", "escrow", "order_router")
    if outs:
        infos = [reg.contract(c) for c in cps]
        if any(i and i.kind in order_kinds for i in infos) or (tx_to_info and tx_to_info.kind in order_kinds):
            all_legs(Cls.ORDER_PLACED, "escrow",
                     f"tokens sent via {tx_to_info.name if tx_to_info else 'order contract'} to an order vault with nothing back")
        elif any(i and i.kind == "bridge" for i in infos) or (tx_to_info and tx_to_info.kind == "bridge"):
            all_legs(Cls.OTHER, "bridge_out", "tokens sent to a registered bridge")
        elif sent and cps == {ZERO} and all(m.asset_kind == AssetKind.ERC20 for m in real):
            all_legs(Cls.OTHER, "bridge_out", "token burned to the zero address by the wallet (bridge unwrap, "
                     "e.g. Avalanche Bridge .e token back to Ethereum)", conf=0.95)
        elif _stake_withdraw_out(tx, wallet, cps, ctx):
            all_legs(Cls.TRANSFER, "to_eoa", "withdrawn from this stake account to an address outside the wallet list "
                     "(the stake's withdraw authority)")
        elif any(i and i.kind == "staking" for i in infos) or (tx_to_info and tx_to_info.kind == "staking"):
            all_legs(Cls.OTHER, "stake", f"tokens sent to staking contract {protocol}")
        else:
            internal = [m for m in real if m.counterparty in ctx.wallets]
            to_contract = any(reg.is_contract(c) for c in cps)
            sub = "to_contract" if to_contract else "to_eoa"
            reason = "outflow only, counterparty not in wallet list"
            trusted = ctx.trusted(wallet)
            look = next((x for c in cps if (x := _lookalike(c, trusted))), None)
            if look:
                reason += f" — WARNING: recipient resembles {look[:10]}…{look[-4:]} " \
                          f"({'own wallet' if look in ctx.wallets else 'a previous recipient'}); possible poisoning victim"
            if internal:
                reason += "; some legs go to own wallets"
            all_legs(Cls.TRANSFER, sub, reason, conf=1.0 if not to_contract else 0.9)
        return _with_gas(out, wallet, tx)

    # inflow only
    minted = all(m.counterparty == ZERO for m in real)
    canonical = all(m.asset_id in reg.tokens or m.asset_id == NATIVE_ASSET for m in real)
    unsolicited = not sent and all(m.counterparty != tx.tx_from for m in real)
    if tx_to_info and tx_to_info.kind == "bridge" and not outs:
        all_legs(Cls.OTHER, "bridge_in", f"inflow released by {tx_to_info.name} (bridge claim)")
    elif tx_to_info and tx_to_info.kind in order_kinds:
        if sent:
            all_legs(Cls.ORDER_CANCELLED, "escrow", f"wallet called {tx_to_info.name}; escrowed tokens returned")
        else:
            all_legs(Cls.ORDER_FILLED, "escrow", f"order executed by {tx.tx_from[:10]} via {tx_to_info.name}")
    elif minted and not sent and tx.tx_to in {m.asset_id for m in real}:
        all_legs(Cls.OTHER, "bridge_in", "token minted to wallet by its own contract in a third-party tx (bridge mint)")
    elif tx_to_info and tx_to_info.kind == "staking":
        all_legs(Cls.OTHER, "unstake_or_reward", f"inflow from staking contract {protocol}")
    elif unsolicited and not canonical:
        all_legs(Cls.OTHER, "airdrop", "unsolicited non-canonical token sent by a distributor (not the tx signer)", conf=0.9)
    elif sent and any(reg.is_contract(c) for c in cps):
        all_legs(Cls.OTHER, "claim", "inflow from a contract in a tx the wallet sent (claim/withdraw)", conf=0.9)
    else:
        exch = next((i for i in (reg.contract(c) for c in [*cps, tx.tx_to]) if i and i.kind == "exchange"), None)
        sub = "from_exchange" if exch else ("from_contract" if any(reg.is_contract(c) for c in cps) else "from_eoa")
        all_legs(Cls.TRANSFER, sub, "inflow only, counterparty not in wallet list")
    return _with_gas(out, wallet, tx)


def _lp_kind(legs: list[Movement], net: dict[str, Decimal], reg: Registry) -> tuple[Cls, str, float] | None:
    """LP mint/burn. Registry-confirmed LP assets are certain; the generic pair-token pattern gets 0.9."""
    for m in legs:
        base = m.asset_id.split(":")[0]
        info = reg.contract(base)
        is_lp_asset = info is not None and info.kind in ("lp_pool", "lp_nft", "lb_pair")
        others_out = any(v < 0 for a, v in net.items() if a != m.asset_id)
        others_in = any(v > 0 for a, v in net.items() if a != m.asset_id)
        if m.amount > 0 and m.counterparty == ZERO and others_out:
            if is_lp_asset:
                return Cls.LP_DEPOSIT, f"LP position minted ({info.name if info else base})", 1.0
            if any(o.counterparty == base for o in legs if o.amount < 0):
                return Cls.LP_DEPOSIT, "LP token minted by the pair the underlying was sent to", 0.9
        if m.amount < 0 and (m.counterparty in (ZERO, base)) and others_in:
            if is_lp_asset:
                return Cls.LP_WITHDRAW, f"LP position burned ({info.name if info else base})", 1.0
            if m.counterparty == base and m.asset_kind == AssetKind.ERC20:
                return Cls.LP_WITHDRAW, "LP token returned to its pair and underlying received", 0.9
    return None


def _role(m: Movement, net: dict[str, Decimal]) -> str:
    if m.source == "min_ada":
        return "min_ada"
    v = net.get(m.asset_id, Decimal(0))
    if v == 0:
        return "passthrough"
    return "in" if v > 0 else "out"


def _syms(legs: list[Movement], assets: set[str]) -> str:
    seen: dict[str, str] = {}
    for m in legs:
        if m.asset_id in assets:
            seen[m.asset_id] = m.symbol
    return "+".join(sorted(seen.values()))


def _note_unknown(addr: str, ctx: RuleContext) -> None:
    reg = ctx.registry
    if addr and addr not in reg.contracts and addr not in reg.tokens and addr not in ctx.wallets \
            and reg.is_contract(addr) and addr.startswith("0x"):
        ctx.unknown_contracts[addr] += 1


def _with_gas(rows: list[Classified], wallet: str, tx: TxContext) -> list[Classified]:
    if rows and tx.tx_from == wallet:
        rows[0].gas_fee_native = tx.gas_fee
    _mark_fees(rows)
    return rows


def _stake_withdraw_out(tx: TxContext, wallet: str, cps: set[str], ctx: RuleContext) -> bool:
    """Solana: a stake `withdraw` out of this (stake) account whose destination is not a listed wallet."""
    ixs = tx.meta.get("stake_ix") or []
    return bool(ixs) and all(i.get("type") == "withdraw" and i.get("stake") == wallet for i in ixs) \
        and all(i.get("destination") not in ctx.wallets for i in ixs) and bool(cps)


def dedupe_gas(rows: list[Classified]) -> int:
    """A tx can be classified in several groups for one wallet (e.g. a Cardano reward withdrawal plus
    the spend it funds); the fee was paid once, so keep it on the first row only. Returns rows cleared."""
    seen: set[tuple[str, str, str]] = set()
    n = 0
    for r in rows:
        if r.gas_fee_native is None:
            continue
        k = (r.movement.chain, r.movement.tx_hash, r.movement.wallet)
        if k in seen:
            r.gas_fee_native = None
            n += 1
        seen.add(k)
    return n


def link_same_tx_internal(rows: list[Classified]) -> int:
    """An outflow whose exact amount lands in another listed wallet in the same tx is internal, whatever
    the sender-side counterparty says (Solana: the recipient's token account, not its owner). The native
    rent the sender paid in that tx to the same token account is booked as account rent."""
    ins: dict[tuple[str, str, str, Decimal], list[Classified]] = defaultdict(list)
    for r in rows:
        m = r.movement
        if m.amount > 0:
            ins[(m.chain, m.tx_hash, m.asset_id, m.amount)].append(r)
    n = 0
    for r in rows:
        m = r.movement
        if m.amount >= 0 or r.classification != Cls.TRANSFER:
            continue
        hit = next((x for x in ins.get((m.chain, m.tx_hash, m.asset_id, -m.amount), [])
                    if x.movement.wallet != m.wallet), None)
        if hit is None:
            continue
        r.classification, r.subtype, r.confidence = Cls.INTERNAL_TRANSFER, "", 1.0
        r.reason = (f"the same amount arrived in listed wallet {hit.movement.wallet[:8]}… in this tx "
                    f"(counterparty {m.counterparty[:8]}… is its token account)")
        n += 1
        for o in rows:
            om = o.movement
            if om.chain == m.chain and om.tx_hash == m.tx_hash and om.wallet == m.wallet and om is not m \
                    and om.asset_id == NATIVE_ASSET and om.amount < 0 and om.counterparty == m.counterparty \
                    and o.classification == Cls.TRANSFER:
                o.classification, o.subtype, o.confidence = Cls.OTHER, "account_rent", 1.0
                o.reason = "rent deposit for the recipient's new token account, paid with the transfer"
    return n


def _mark_fees(rows: list[Classified]) -> None:
    """Within a swap/fill, a small extra outflow of a sold asset to a different counterparty is a fee leg."""
    by_asset: dict[str, list[Classified]] = defaultdict(list)
    for r in rows:
        if r.classification in (Cls.SWAP, Cls.ORDER_FILLED) and r.movement.amount < 0:
            by_asset[r.movement.asset_id].append(r)
    for rs in by_asset.values():
        if len(rs) < 2:
            continue
        total = -sum((r.movement.amount for r in rs), Decimal(0))
        main = min(rs, key=lambda r: r.movement.amount)  # largest outflow
        for r in rs:
            if r is not main and r.movement.counterparty != main.movement.counterparty \
                    and -r.movement.amount <= total * FEE_SHARE:
                r.leg_role = "fee"


def classify_wallet(wallet: str, moves: list[Movement], txs: dict[str, TxContext], ctx: RuleContext) -> list[Classified]:
    groups: dict[str, list[Movement]] = defaultdict(list)
    for m in moves:
        groups[m.tx_hash].append(m)
    for h, t in txs.items():
        if t.tx_from == wallet and h not in groups:
            groups[h] = []
    order = sorted(groups, key=lambda h: (txs[h].timestamp, txs[h].block, h))
    ctx.recipients[wallet] = trusted_recipients(wallet, order, groups, txs, ctx.wallets)
    rows: list[Classified] = []
    for h in order:
        rows.extend(classify_group(wallet, txs[h], groups[h], ctx))
    link_escrow_orders(rows)
    return rows


def trusted_recipients(wallet: str, order: list[str], groups: dict[str, list[Movement]],
                       txs: dict[str, TxContext], wallets: set[str]) -> set[str]:
    """Addresses the wallet itself sent value to, in time order. A recipient that resembles an
    already-trusted address is *not* trusted (it is the likely poisoning victim case)."""
    trusted: set[str] = set()
    for h in order:
        if txs[h].tx_from != wallet:
            continue
        for m in groups[h]:
            cp = m.counterparty
            if m.amount < 0 and _addr_like(cp) and cp not in trusted \
                    and not _lookalike(cp, wallets | trusted):
                trusted.add(cp)
    return trusted


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def link_escrow_orders(rows: list[Classified]) -> None:
    """Link escrow ORDER_PLACED rows to the later cancel/fill that empties the same vault.

    The placement's counterparty is the per-order vault. A cancel refunds from that vault; a fill
    tx contains a Transfer log whose `from` is the vault. Both are exact (confidence stays 1.0).
    """
    placed = {r.movement.counterparty: r for r in rows
              if r.classification == Cls.ORDER_PLACED and r.subtype == "escrow"}
    if not placed:
        return
    for r in rows:
        if r.classification not in (Cls.ORDER_CANCELLED, Cls.ORDER_FILLED) or r.subtype != "escrow":
            continue
        vaults = {r.movement.counterparty} | {
            "0x" + lg.topics[1][-40:] for lg in r.tx.logs
            if len(lg.topics) == 3 and lg.topics[0] == TRANSFER_TOPIC}
        for v in vaults:
            p = placed.get(v)
            if p and p.tx.timestamp <= r.tx.timestamp:
                r.linked_tx_hash = p.tx.tx_hash
                r.reason += f"; empties order vault {v[:10]} funded by {p.tx.tx_hash[:10]}"
                p.linked_tx_hash = r.tx.tx_hash
                p.subtype = "escrow_" + ("cancelled" if r.classification == Cls.ORDER_CANCELLED else "filled")
                break
    for p in placed.values():
        if not p.linked_tx_hash:
            p.subtype = "escrow_open_or_unknown"
