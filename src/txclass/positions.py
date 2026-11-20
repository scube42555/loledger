"""LP / yield position ledger with cost basis, built from classified rows.

Cardano LP (batcher orders): the underlying leaves the wallet in the ORDER_PLACED tx and the LP
token arrives in the LP_DEPOSIT tx (same order_id). Cost basis = USD value of everything the
wallet sent for that order (incl. batcher fee/deposit) minus non-LP assets that came back.
Withdrawals mirror it: LP tokens sent with an lp_remove order, underlying received in LP_WITHDRAW.
Direct (single-tx) LP deposits/withdrawals on EVM use the same tx's legs.
Yield vaults (e.g. Tangem Aave): OTHER/yield_deposit and yield_withdraw per (wallet, vault, asset).

Withdrawals are linked to the deposits of the same position (all prior deposits, FIFO by time).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .model import NATIVE_ASSET
from .rules import Classified, Cls


@dataclass
class PosEvent:
    ts: int
    kind: str  # deposit | withdraw | fees
    tx: str
    lp_qty: Decimal
    assets: dict[str, Decimal]  # underlying symbol -> signed amount from the wallet's view
    usd: Decimal | None  # signed USD of underlying (deposit < 0, withdraw > 0)


@dataclass
class Position:
    key: str
    chain: str
    wallet: str
    venue: str
    lp_symbol: str
    events: list[PosEvent] = field(default_factory=list)

    def _sum(self, kind: str) -> tuple[dict[str, Decimal], Decimal | None, Decimal]:
        amounts: dict[str, Decimal] = defaultdict(Decimal)
        usd: Decimal | None = Decimal(0)
        lp = Decimal(0)
        for e in self.events:
            if e.kind != kind:
                continue
            for a, v in e.assets.items():
                amounts[a] += v
            usd = None if usd is None or e.usd is None else usd + e.usd
            lp += e.lp_qty
        return dict(amounts), usd, lp

    def summary(self) -> dict[str, str]:
        dep_a, dep_usd, lp_in = self._sum("deposit")
        wd_a, wd_usd, lp_out = self._sum("withdraw")
        fee_a, fee_usd, _ = self._sum("fees")
        lp_net = lp_in - lp_out
        is_yield = self.lp_symbol.startswith("vault:")
        if is_yield:
            closed = sum(wd_a.values(), Decimal(0)) + sum(dep_a.values(), Decimal(0)) >= 0 and bool(wd_a)
        else:
            closed = lp_in > 0 and lp_net <= lp_in * Decimal("0.0001")
        cost = -dep_usd if dep_usd is not None else None
        back = (wd_usd or Decimal(0)) + (fee_usd or Decimal(0)) if wd_usd is not None and fee_usd is not None else None
        pnl = back - cost if (closed and back is not None and cost is not None) else None

        def fmt(d: dict[str, Decimal]) -> str:
            return ", ".join(f"{abs(v).normalize():f} {k}" for k, v in sorted(d.items()) if v) or "-"

        deps = [e for e in self.events if e.kind == "deposit"]
        wds = [e for e in self.events if e.kind == "withdraw"]
        return {
            "position": self.key, "chain": self.chain, "wallet": self.wallet, "venue": self.venue,
            "lp_token": self.lp_symbol, "status": "closed" if closed else "open",
            "opened": str(min((e.ts for e in self.events), default=0)),
            "deposits": str(len(deps)), "withdrawals": str(len(wds)),
            "lp_in": f"{lp_in.normalize():f}", "lp_out": f"{lp_out.normalize():f}",
            "deposited": fmt(dep_a), "withdrawn": fmt(wd_a), "fees_claimed": fmt(fee_a),
            "cost_basis_usd": "" if cost is None else f"{cost:.2f}",
            "withdrawn_usd": "" if wd_usd is None else f"{wd_usd:.2f}",
            "realized_pnl_usd": "" if pnl is None else f"{pnl:.2f}",
            "deposit_txs": " ".join(e.tx for e in deps), "withdraw_txs": " ".join(e.tx for e in wds),
        }


def _usd(rows: list[Classified]) -> Decimal | None:
    total = Decimal(0)
    for r in rows:
        if r.usd_value is None:
            if r.movement.amount != 0:
                return None
            continue
        total += r.usd_value
    return total


def _minted(r: Classified) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for m in r.tx.meta.get("minted", []):
        out[m["asset"]] += int(m["qty"])
    return out


def build_positions(rows: list[Classified]) -> list[Position]:
    by_order: dict[str, list[Classified]] = defaultdict(list)
    for r in rows:
        if r.order_id:
            by_order[r.order_id].append(r)
    positions: dict[str, Position] = {}
    linked: dict[tuple[str, str], list[Classified]] = defaultdict(list)
    for r in rows:
        if r.classification in (Cls.LP_DEPOSIT, Cls.LP_WITHDRAW, Cls.LP_FEES_CLAIMED):
            linked[(r.movement.wallet, r.tx.tx_hash)].append(r)

    for (wallet, h), rs in linked.items():
        kind = {Cls.LP_DEPOSIT: "deposit", Cls.LP_WITHDRAW: "withdraw", Cls.LP_FEES_CLAIMED: "fees"}[rs[0].classification]
        placed = [p for p in by_order.get(rs[0].order_id, []) if p.classification == Cls.ORDER_PLACED] if rs[0].order_id else []
        legs = [*placed, *rs]
        minted = _minted(rs[0])
        if kind == "deposit":
            cands = [r for r in rs if r.movement.amount > 0 and r.movement.asset_id != NATIVE_ASSET
                     and r.movement.source != "min_ada"]
            # the LP token is the one minted in this tx (Cardano) or minted from the zero address (EVM)
            lp_legs = [r for r in cands if minted.get(r.movement.asset_id, 0) > 0
                       or r.movement.counterparty == "0x0000000000000000000000000000000000000000"] or \
                [r for r in cands if r.usd_value is None][:1]
            under = [r for r in legs if r not in lp_legs]
        elif kind == "withdraw":
            cands = [r for r in legs if r.movement.amount < 0 and r.movement.asset_id != NATIVE_ASSET
                     and r.movement.source != "min_ada"]
            lp_legs = [r for r in cands if minted.get(r.movement.asset_id, 0) < 0] or \
                [r for r in cands if r.usd_value is None][:1]
            under = [r for r in legs if r not in lp_legs]
        else:
            lp_legs, under = [], rs
        if not lp_legs and kind != "fees":
            continue
        lp = lp_legs[0].movement if lp_legs else rs[0].movement
        key = f"{lp.chain}:{wallet}:{lp.asset_id}"
        pos = positions.setdefault(key, Position(key, lp.chain, wallet, rs[0].protocol, lp.symbol))
        assets: dict[str, Decimal] = defaultdict(Decimal)
        for u in under:
            assets[u.movement.symbol] += u.movement.amount
        pos.events.append(PosEvent(rs[0].tx.timestamp, kind, h, sum((abs(x.movement.amount) for x in lp_legs), Decimal(0)),
                                   dict(assets), _usd(under)))

    for r in rows:  # yield vaults
        if r.classification == Cls.OTHER and r.subtype in ("yield_deposit", "yield_withdraw"):
            vault = r.protocol or r.tx.tx_to
            key = f"{r.movement.chain}:{r.movement.wallet}:{vault}:{r.movement.asset_id}"
            pos = positions.setdefault(key, Position(key, r.movement.chain, r.movement.wallet, vault,
                                                     f"vault:{r.movement.symbol}"))
            pos.events.append(PosEvent(r.tx.timestamp, "deposit" if r.subtype == "yield_deposit" else "withdraw",
                                       r.tx.tx_hash, Decimal(0), {r.movement.symbol: r.movement.amount},
                                       r.usd_value))
    for r in rows:  # Avalanche C-Chain <-> P/X-Chain round trips (staking/delegation is the usual reason)
        if r.classification == Cls.OTHER and r.subtype in ("atomic_export", "atomic_import"):
            key = f"{r.movement.chain}:{r.movement.wallet}:avalanche-p/x-chain"
            pos = positions.setdefault(key, Position(key, r.movement.chain, r.movement.wallet, "avalanche-atomic",
                                                     "vault:AVAX on P/X-Chain"))
            pos.events.append(PosEvent(r.tx.timestamp, "deposit" if r.subtype == "atomic_export" else "withdraw",
                                       r.tx.tx_hash, Decimal(0), {r.movement.symbol: r.movement.amount}, r.usd_value))
    for r in rows:  # Ethereum validators: deposit and withdrawal addresses often differ, so pool per chain
        if r.protocol == "eth2-staking" and r.movement.amount != 0:
            key = f"{r.movement.chain}:all-wallets:eth2-staking"
            pos = positions.setdefault(key, Position(key, r.movement.chain, "all wallets", "eth2-staking",
                                                     "vault:ETH validator(s)"))
            pos.events.append(PosEvent(r.tx.timestamp, "deposit" if r.movement.amount < 0 else "withdraw",
                                       r.tx.tx_hash, Decimal(0), {r.movement.symbol: r.movement.amount}, r.usd_value))
    for p in positions.values():
        p.events.sort(key=lambda e: e.ts)
    return sorted(positions.values(), key=lambda p: (p.chain, p.wallet, p.events[0].ts if p.events else 0))
