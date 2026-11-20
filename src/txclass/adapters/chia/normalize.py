"""Chia coin records -> wallet-perspective Movements for one receive address (puzzle hash).

Spends: all of the address's coins spent at a height form one 'tx'. The block's additions whose
parent is one of those coins are the spend's outputs; outputs back to the address are change,
others are out legs (leg_id coin:<coin id>, which is what exchanges report as the deposit tx id).
fee = spent - outputs. Receipts: coins created at the address whose parent is not ours (counterparty
= parent's puzzle hash). Reconciles: unspent balance == sum(legs) - fees.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale
from .fetch import ChiaClient, coin_id, puzzle_hash_to_address

DEC = 12
FARMING_CP = "chia:farming_reward"
BUNDLE_CP = "chia:other_inputs_in_same_spend"


def _with_source(m: Movement, source: str) -> Movement:
    import dataclasses

    return dataclasses.replace(m, source=source)


def normalize(chain: ChainConfig, wallet: str, puzzle_hash: str, records: list[dict[str, Any]], client: ChiaClient
              ) -> tuple[list[Movement], dict[str, TxContext]]:
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    ours = {coin_id(r["coin"]["parent_coin_info"], r["coin"]["puzzle_hash"], int(r["coin"]["amount"])): r for r in records}
    spent_at: dict[int, list[str]] = defaultdict(list)
    for name, r in ours.items():
        if r.get("spent"):
            spent_at[int(r["spent_block_index"])].append(name)

    def mk(h: str, leg: str, ts: int, height: int, raw: int, cp: str) -> Movement:
        return Movement(chain=chain.name, tx_hash=h, leg_id=leg, block=height, timestamp=ts, wallet=wallet,
                        asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE, symbol="XCH", decimals=DEC,
                        amount=scale(raw, DEC), counterparty=cp, source="chia")

    change: set[str] = set()
    for height, names in sorted(spent_at.items()):
        blk = client.block(height)
        ts = int(blk.get("timestamp") or 0)
        ar = client.additions_removals(str(blk.get("header_hash")))
        outs = [a for a in ar.get("additions") or [] if a["coin"]["parent_coin_info"] in names]
        spent_amt = sum(int(ours[n]["coin"]["amount"]) for n in names)
        out_amt = sum(int(a["coin"]["amount"]) for a in outs)
        h = f"chia:{height}:{sorted(names)[0][:18]}"
        legs = []
        for a in outs:
            cid = coin_id(a["coin"]["parent_coin_info"], a["coin"]["puzzle_hash"], int(a["coin"]["amount"]))
            if a["coin"]["puzzle_hash"] == puzzle_hash:
                change.add(cid)
                continue
            legs.append(mk(h, f"coin:{cid}", ts, height, -int(a["coin"]["amount"]), puzzle_hash_to_address(a["coin"]["puzzle_hash"])))
        fee = spent_amt - out_amt
        if not outs and spent_amt > 0:
            # nothing attached to our coins: a standard wallet hung all outputs on a coin from another of its
            # addresses, so ours were pooled into that spend (paired across wallets by pair_bundles)
            legs.append(mk(h, "bundle_contribution", ts, height, -spent_amt, BUNDLE_CP))
            fee = 0
        elif fee < 0:
            # a standard wallet attaches all outputs to one coin; coins from the wallet's other (unlisted)
            # addresses funded the rest of this spend
            legs.append(mk(h, "bundle_inputs", ts, height, -fee, BUNDLE_CP))
            fee = 0
        moves.extend(legs)
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=height, timestamp=ts, tx_from=wallet,
                           tx_to=legs[0].counterparty if legs else wallet, method_id="",
                           function_name="" if legs else "consolidation", is_error=False,
                           gas_fee=scale(fee, DEC), value=Decimal(0),
                           meta={"spent_coins": names, "output_coins": [m.leg_id[5:] for m in legs]})
    for name, r in ours.items():
        if name in change:
            continue
        parent = r["coin"]["parent_coin_info"]
        if parent in ours:  # created by our own spend (e.g. change in a tx not seen above)
            continue
        height = int(r["confirmed_block_index"])
        ts = int(r.get("timestamp") or 0)
        h = f"chia:{height}:{name[:18]}"
        if r.get("coinbase"):  # farmer/pool block reward: synthetic parent id, no sender
            sender = FARMING_CP
            m = mk(h, f"coin:{name}", ts, height, int(r["coin"]["amount"]), sender)
            moves.append(_with_source(m, "farming_reward"))
        else:
            try:
                pc = client.coin(parent)
            except Exception:  # parent pruned/unknown: keep the inflow, sender unknown
                pc = {}
            sender = puzzle_hash_to_address(pc["coin"]["puzzle_hash"]) if pc else "unknown"
            moves.append(mk(h, f"coin:{name}", ts, height, int(r["coin"]["amount"]), sender))
        txs[h] = TxContext(chain=chain.name, tx_hash=h, block=height, timestamp=ts, tx_from=sender, tx_to=wallet,
                           method_id="", function_name="", is_error=False, gas_fee=Decimal(0), value=Decimal(0),
                           meta={"coin": name, "parent": parent})
    moves.sort(key=lambda m: (m.timestamp, m.tx_hash, m.leg_id))
    return moves, txs


def pair_bundles(results: dict[str, tuple[list[Movement], dict[str, TxContext]]]) -> list[str]:
    """Pooled spends across the user's addresses. At a block height where one listed address received the
    surplus of a spend (bundle_inputs) and other listed addresses contributed coins with no outputs attached
    (bundle_contribution), the contributions become internal transfers into the receiver: its bundle_inputs leg
    is split per contributor, and whatever is left came from the wallet's unlisted addresses."""
    contrib: dict[int, list[tuple[str, int]]] = defaultdict(list)
    recv: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for w, (moves, _) in results.items():
        for i, m in enumerate(moves):
            if m.leg_id == "bundle_contribution":
                contrib[m.block].append((w, i))
            elif m.leg_id == "bundle_inputs":
                recv[m.block].append((w, i))
    notes: list[str] = []
    splits: dict[tuple[str, int], list[Movement]] = {}
    for height, cs in sorted(contrib.items()):
        rs = recv.get(height, [])
        if len(rs) != 1 or any(w == rs[0][0] for w, _ in cs):
            continue
        rw, ri = rs[0]
        rm = results[rw][0][ri]
        total = sum((-results[w][0][i].amount for w, i in cs), Decimal(0))
        if total > rm.amount + Decimal("0.01"):
            continue  # contributions exceed what arrived: not one pooled spend
        rest = rm.amount - total
        split: list[Movement] = []
        for w, i in cs:
            cm = results[w][0][i]
            results[w][0][i] = replace(cm, counterparty=rw)
            tx = results[w][1].get(cm.tx_hash)
            if tx is not None:
                results[w][1][cm.tx_hash] = replace(tx, tx_to=rw, function_name="")
            split.append(replace(rm, leg_id=f"bundle_from:{w[:16]}", amount=-cm.amount, counterparty=w))
            notes.append(f"height {height}: {-cm.amount} XCH pooled from {w[:12]}… into {rw[:12]}…'s spend")
        if rest > 0:
            split.append(replace(rm, amount=rest))  # still from the wallet's unlisted addresses
        else:
            # a negative rest is the spend's fee, paid out of the contributions
            results[rw][1][rm.tx_hash] = replace(results[rw][1][rm.tx_hash],
                                                 gas_fee=results[rw][1][rm.tx_hash].gas_fee - rest)
        splits[(rw, ri)] = split
    for (w, i), legs in sorted(splits.items(), key=lambda kv: -kv[0][1]):  # from the end: indices stay valid
        results[w][0][i:i + 1] = legs
    return notes
