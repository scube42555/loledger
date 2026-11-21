"""Cardano eUTXO -> wallet-perspective Movements.

Ownership: an input/output belongs to wallet S iff its payment part is a *key* hash and its
stake address is S. Script addresses that merely carry S's stake key (e.g. order scripts)
are not the wallet's funds.

Wallet-built tx (wallet has inputs): legs are the net change per (external address, asset)
over non-wallet inputs/outputs, plus explicit legs for the stake deposit, reward withdrawals
and minting. With fee paid separately this reconciles exactly:
    wallet_out - wallet_in == sum(legs) - fee        (per asset; fee only for ADA)
Other txs (batcher payouts, plain receipts): legs are the wallet's own outputs per asset.

min-ADA: ADA travelling in the same output as tokens (<= MIN_ADA_MAX) is emitted as a
separate leg with source="min_ada" so rules attach it to the token leg rather than treating
it as an ADA transfer.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale
from .address import parse_address
from .fetch import CardanoRawBundle

log = logging.getLogger(__name__)

LOVELACE = 6
MIN_ADA_MAX = 5_000_000  # lovelace
DEPOSIT_CP = "cardano:deposit"
REWARDS_CP = "cardano:rewards"
MINT_CP = "cardano:mint"
STAKE_REWARD_CP = "cardano:epoch_reward"
REWARD_ACCOUNT_SUFFIX = "#rewards"
SHELLEY_EPOCH, SHELLEY_START, EPOCH_SECONDS = 208, 1596059091, 432000  # mainnet: epoch 208 began 2020-07-29 21:44:51Z


def epoch_start(epoch: int) -> int:
    return SHELLEY_START + (epoch - SHELLEY_EPOCH) * EPOCH_SECONDS


def current_epoch(now: float) -> int:
    return SHELLEY_EPOCH + int(now - SHELLEY_START) // EPOCH_SECONDS


def reward_account(stake: str) -> str:
    """Wallet id of the stake key's reward account: rewards accrue there per epoch and reach the wallet's
    UTxOs only through a withdrawal, which is therefore an internal transfer, not income."""
    return stake + REWARD_ACCOUNT_SUFFIX


def asset_key(a: dict[str, Any]) -> str:
    return f"{a['policy_id']}.{a['asset_name'] or ''}"


def asset_symbol(name_hex: str, policy: str) -> str:
    try:
        s = bytes.fromhex(name_hex).decode("utf-8")
        if s and s.isprintable():
            return s
    except (ValueError, UnicodeDecodeError):
        pass
    return (name_hex[:16] or policy[:8]) + "…"


def _ref(io: dict[str, Any]) -> str:
    return f"{io['tx_hash']}#{io['tx_index']}"


def _values(io: dict[str, Any]) -> dict[str, int]:
    v = {NATIVE_ASSET: int(io["value"])}
    for a in io.get("asset_list") or []:
        k = asset_key(a)
        v[k] = v.get(k, 0) + int(a["quantity"])
    return v


class _Owner:
    def __init__(self, stake: str, all_wallets: set[str]) -> None:
        self.stake = stake
        self.all = all_wallets

    def wallet_of(self, io: dict[str, Any]) -> str | None:
        """Which listed wallet (if any) controls this input/output."""
        addr = io["payment_addr"]["bech32"]
        pa = parse_address(addr)
        if pa is None or not pa.is_key_payment:
            return None
        st = io.get("stake_addr")
        if st and st in self.all:
            return str(st)
        if addr in self.all:  # enterprise address listed directly
            return str(addr)
        return None


def _datum_value(o: dict[str, Any], datums: dict[str, Any]) -> Any:
    if o.get("inline_datum"):
        return o["inline_datum"].get("value")
    h = o.get("datum_hash")
    return datums.get(h) if h else None


def build_tx(chain: ChainConfig, t: dict[str, Any], datums: dict[str, Any], owner: _Owner) -> TxContext:
    msg = (t.get("metadata") or {}).get("674", {})
    msg_list = msg.get("msg", []) if isinstance(msg, dict) else []
    if isinstance(msg_list, str):
        msg_list = [msg_list]
    signer = ""
    for i in t["inputs"]:
        pa = parse_address(i["payment_addr"]["bech32"])
        if pa and pa.is_key_payment:
            signer = owner.wallet_of(i) or i.get("stake_addr") or i["payment_addr"]["bech32"]
            break
    script_outputs: dict[str, Any] = {}
    for o in t["outputs"]:
        pa = parse_address(o["payment_addr"]["bech32"])
        if pa and pa.is_script_payment:
            script_outputs[_ref(o)] = {
                "address": o["payment_addr"]["bech32"], "script_hash": pa.payment_cred,
                "stake": o.get("stake_addr"), "datum_hash": o.get("datum_hash"),
                "datum": _datum_value(o, datums), "values": {k: str(v) for k, v in _values(o).items()},
            }
    script_inputs = {_ref(i): i["payment_addr"]["bech32"] for i in t["inputs"]
                     if (pa := parse_address(i["payment_addr"]["bech32"])) is not None and pa.is_script_payment}
    to = max(script_outputs.values(), key=lambda s: int(s["values"][NATIVE_ASSET]))["address"] if script_outputs else (
        t["outputs"][0]["payment_addr"]["bech32"] if t["outputs"] else "")
    return TxContext(
        chain=chain.name, tx_hash=t["tx_hash"], block=int(t["block_height"]), timestamp=int(t["tx_timestamp"]),
        tx_from=signer, tx_to=to, method_id="", function_name=" | ".join(str(m) for m in msg_list),
        is_error=False, gas_fee=scale(t["fee"], LOVELACE), value=Decimal(0),
        meta={
            "msg": msg_list, "inputs": [_ref(i) for i in t["inputs"]], "script_inputs": script_inputs,
            "script_outputs": script_outputs,
            "withdrawals": [{"stake": w["stake_addr"], "amount": w["amount"]} for w in t.get("withdrawals") or []],
            "certs": [{"type": c.get("type"), "info": c.get("info")} for c in t.get("certificates") or []],
            "minted": [{"asset": asset_key(a), "qty": a["quantity"]} for a in t.get("assets_minted") or []],
            "deposit": str(t.get("deposit") or 0),
        },
    )


def normalize(chain: ChainConfig, bundle: CardanoRawBundle, all_wallets: set[str]) -> tuple[list[Movement], dict[str, TxContext]]:
    S = bundle.stake
    owner = _Owner(S, all_wallets)
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    meta_assets: dict[str, tuple[str, int]] = {}

    for t in bundle.txs.values():
        for io in [*t["inputs"], *t["outputs"]]:
            for a in io.get("asset_list") or []:
                meta_assets[asset_key(a)] = (asset_symbol(a["asset_name"] or "", a["policy_id"]), int(a.get("decimals") or 0))
        for a in t.get("assets_minted") or []:
            meta_assets.setdefault(asset_key(a), (asset_symbol(a["asset_name"] or "", a["policy_id"]), int(a.get("decimals") or 0)))

    def mk(t: dict[str, Any], leg: str, asset: str, raw: int, cp: str, *, refs: list[str], source: str,
           datum: str = "") -> Movement:
        if asset == NATIVE_ASSET:
            sym, dec, kind = chain.native_symbol, LOVELACE, AssetKind.NATIVE
        else:
            sym, dec = meta_assets.get(asset, (asset[57:73] or asset[:8], 0))
            kind = AssetKind.CARDANO
        return Movement(chain=chain.name, tx_hash=t["tx_hash"], leg_id=leg, block=int(t["block_height"]),
                        timestamp=int(t["tx_timestamp"]), wallet=S, asset_id=asset, asset_kind=kind, symbol=sym,
                        decimals=dec, amount=scale(raw, dec), counterparty=cp, source=source,
                        utxo_ref=";".join(refs), datum_hash=datum)

    for t in sorted(bundle.txs.values(), key=lambda x: (x["block_height"], x.get("tx_block_index", 0))):
        tx = build_tx(chain, t, bundle.datums, owner)
        txs[tx.tx_hash] = tx
        w_in: dict[str, int] = defaultdict(int)
        w_out: dict[str, int] = defaultdict(int)
        nw: dict[tuple[str, str], int] = defaultdict(int)  # (address, asset) -> in - out (wallet perspective)
        refs_in: dict[str, list[str]] = defaultdict(list)
        refs_out: dict[str, list[str]] = defaultdict(list)
        datum_of: dict[str, str] = {}
        token_outputs: dict[str, int] = defaultdict(int)  # address -> lovelace in token-carrying outputs
        token_inputs: dict[str, int] = defaultdict(int)  # same for non-wallet inputs
        for i in t["inputs"]:
            vals = _values(i)
            if owner.wallet_of(i) == S:
                for k, v in vals.items():
                    w_in[k] += v
            else:
                addr = i["payment_addr"]["bech32"]
                refs_in[addr].append(_ref(i))
                if len(vals) > 1 and vals[NATIVE_ASSET] <= MIN_ADA_MAX:
                    token_inputs[addr] += vals[NATIVE_ASSET]
                for k, v in vals.items():
                    nw[(addr, k)] += v
        for o in t["outputs"]:
            vals = _values(o)
            if owner.wallet_of(o) == S:
                for k, v in vals.items():
                    w_out[k] += v
            else:
                addr = o["payment_addr"]["bech32"]
                refs_out[addr].append(_ref(o))
                if o.get("datum_hash") or o.get("inline_datum"):
                    datum_of[addr] = o.get("datum_hash") or "inline"
                if len(vals) > 1 and vals[NATIVE_ASSET] <= MIN_ADA_MAX:
                    token_outputs[addr] += vals[NATIVE_ASSET]
                for k, v in vals.items():
                    nw[(addr, k)] -= v

        signed = bool(w_in)
        if not signed and not w_out:
            continue  # tx touches only scripts carrying our stake key (e.g. order fills paid elsewhere)

        cp_id = {a: (owner.wallet_of({"payment_addr": {"bech32": a}, "stake_addr": _stake_of(t, a)}) or a)
                 for a in {a for a, _ in nw}}
        legs: list[Movement] = []
        if signed:
            n = 0
            for (addr, asset), v in sorted(nw.items()):
                if v == 0:
                    continue
                n += 1
                refs = refs_out[addr] if v < 0 else refs_in[addr]
                src = "utxo"
                if asset == NATIVE_ASSET and v < 0 and -v <= token_outputs.get(addr, 0) and \
                        any(a == addr and k != NATIVE_ASSET and val < 0 for (a, k), val in nw.items()):
                    src = "min_ada"
                if asset == NATIVE_ASSET and v > 0 and v <= token_inputs.get(addr, 0) and \
                        any(a == addr and k != NATIVE_ASSET and val > 0 for (a, k), val in nw.items()):
                    src = "min_ada"
                legs.append(mk(t, f"utxo:{n}", asset, v, cp_id[addr], refs=refs, source=src,
                               datum=datum_of.get(addr, "") if v < 0 else ""))
            dep = int(t.get("deposit") or 0)
            if dep:
                legs.append(mk(t, "deposit", NATIVE_ASSET, -dep, DEPOSIT_CP, refs=[], source="deposit"))
            wd = sum(int(w["amount"]) for w in t.get("withdrawals") or [] if w["stake_addr"] == S)
            if wd:
                legs.append(mk(t, "rewards", NATIVE_ASSET, wd, REWARDS_CP, refs=[], source="reward_withdrawal"))
            for a in t.get("assets_minted") or []:
                legs.append(mk(t, f"mint:{asset_key(a)[:16]}", asset_key(a), int(a["quantity"]), MINT_CP, refs=[],
                               source="mint"))
            # reconcile: wallet_out - wallet_in == sum(legs) - fee
            fee = int(t["fee"])
            assets = set(w_in) | set(w_out) | {m.asset_id for m in legs}
            for asset in assets:
                legsum = sum(int(m.amount.scaleb(m.decimals)) for m in legs if m.asset_id == asset)
                expect = w_out.get(asset, 0) - w_in.get(asset, 0) + (fee if asset == NATIVE_ASSET else 0)
                if legsum != expect:
                    log.warning("  %s: unbalanced %s by %d; adding residual leg", t["tx_hash"][:12], asset[:20],
                                expect - legsum)
                    legs.append(mk(t, f"residual:{asset[:16]}", asset, expect - legsum, "cardano:unknown", refs=[],
                                   source="residual"))
        else:
            # counterparty: the script UTxO being spent (order/pool) if any, else the first input's owner
            script_in = next((i for i in t["inputs"]
                              if (pa := parse_address(i["payment_addr"]["bech32"])) and pa.is_script_payment), None)
            first = script_in or (t["inputs"][0] if t["inputs"] else None)
            if first is None:
                sender = ""
            elif script_in is not None:
                sender = script_in["payment_addr"]["bech32"]
            else:
                sender = owner.wallet_of(first) or first.get("stake_addr") or first["payment_addr"]["bech32"]
            outs = [o for o in t["outputs"] if owner.wallet_of(o) == S]
            n = 0
            for asset, v in sorted((k, w_out[k] - w_in.get(k, 0)) for k in w_out):
                if v == 0:
                    continue
                n += 1
                refs = [_ref(o) for o in outs if asset in _values(o)]
                src = "utxo"
                if asset == NATIVE_ASSET and all(len(_values(o)) > 1 and int(o["value"]) <= MIN_ADA_MAX for o in outs):
                    src = "min_ada"
                legs.append(mk(t, f"utxo:{n}", asset, v, str(sender), refs=refs, source=src))
        moves.extend(legs)

    moves.sort(key=lambda m: (m.timestamp, m.block, m.tx_hash, m.leg_id))
    return moves, txs


def _stake_of(t: dict[str, Any], addr: str) -> str | None:
    for io in [*t["inputs"], *t["outputs"]]:
        if io["payment_addr"]["bech32"] == addr:
            st = io.get("stake_addr")
            return str(st) if st else None
    return None


def reward_account_movements(chain: ChainConfig, stake: str, rewards: list[dict[str, Any]], txs: dict[str, TxContext],
                             epoch_now: int) -> tuple[list[Movement], dict[str, TxContext]]:
    """Reward account ledger: +reward per earned epoch (dated at the start of its spendable epoch, earned+2,
    when the ledger credits it) and -amount for each withdrawal tx of this stake key."""
    acct = reward_account(stake)
    moves: list[Movement] = []
    out: dict[str, TxContext] = {}
    for r in rewards:
        sp = int(r["spendable_epoch"])
        if sp > epoch_now:  # earned but not credited yet
            continue
        ts, kind, pool = epoch_start(sp), str(r.get("type") or "member"), str(r.get("pool_id_bech32") or "")
        h = f"ada-reward:{r['earned_epoch']}:{kind}:{stake}"
        moves.append(Movement(chain=chain.name, tx_hash=h, leg_id="reward", block=0, timestamp=ts, wallet=acct,
                              asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol=chain.native_symbol,
                              decimals=LOVELACE, amount=scale(int(r["amount"]), LOVELACE), counterparty=pool or STAKE_REWARD_CP,
                              source="staking_reward"))
        out[h] = TxContext(chain=chain.name, tx_hash=h, block=0, timestamp=ts, tx_from="", tx_to=acct, method_id="",
                           function_name="epoch_reward", is_error=False, gas_fee=Decimal(0), value=Decimal(0),
                           meta={"epoch": int(r["earned_epoch"]), "spendable_epoch": sp, "reward_type": kind, "pool": pool})
    for t in txs.values():
        wd = sum(int(w["amount"]) for w in t.meta.get("withdrawals") or [] if w["stake"] == stake)
        if wd:
            moves.append(Movement(chain=chain.name, tx_hash=t.tx_hash, leg_id="rewards_out", block=t.block,
                                  timestamp=t.timestamp, wallet=acct, asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE,
                                  symbol=chain.native_symbol, decimals=LOVELACE, amount=-scale(wd, LOVELACE),
                                  counterparty=stake, source="reward_withdrawal"))
    return moves, out
