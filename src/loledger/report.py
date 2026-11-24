"""classified.csv writer and console summary."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .model import AssetKind
from .positions import Position
from .rules import Classified, Cls

COLUMNS = [
    "chain", "timestamp", "date_utc", "block", "tx_hash", "leg_id", "wallet", "wallet_label", "direction",
    "token", "token_address", "amount", "counterparty", "usd_value_at_time", "classification", "subtype",
    "leg_role", "linked_tx_hash", "protocol", "confidence", "reason", "gas_fee_native", "gas_fee_usd",
    "tx_from", "tx_to", "utxo_ref", "datum_summary", "order_id", "order_details", "usd_price", "price_source",
    "withdrawal_address",  # exchange withdrawals: the destination address the exchange reports
    "withdrawal_network",  # ... and the network it names ("" when the export doesn't say)
]


def _usd(v: Decimal | None) -> str:
    return "" if v is None else f"{v:.2f}"


def wallet_label(labels: dict[str, str], chain: str, wallet: str) -> str:
    """The addresses.csv label of the row for this chain; an EVM address listed on several chains has one per chain."""
    return labels.get(f"{chain}|{wallet}") or labels.get(wallet, "")


def write_csv(path: Path, rows: Iterable[Classified], labels: dict[str, str]) -> int:
    n = 0
    with path.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(COLUMNS)
        for r in rows:
            m, t = r.movement, r.tx
            wr.writerow([
                m.chain, m.timestamp, datetime.fromtimestamp(m.timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S"),
                m.block, m.tx_hash, m.leg_id, m.wallet, wallet_label(labels, m.chain, m.wallet),
                "" if m.amount == 0 else m.direction.value, m.symbol, m.asset_id, f"{m.amount:f}",
                m.counterparty, _usd(r.usd_value), r.classification.value, r.subtype, r.leg_role, r.linked_tx_hash,
                r.protocol, f"{r.confidence:.2f}", r.reason,
                "" if r.gas_fee_native is None else f"{r.gas_fee_native:f}", _usd(r.gas_fee_usd),
                t.tx_from, t.tx_to, m.utxo_ref, r.datum_summary, r.order_id, r.order_details,
                "" if r.usd_price is None else f"{r.usd_price:.10g}", r.price_source,
                str(t.meta.get("address") or "") if m.asset_kind == AssetKind.EXCHANGE and m.amount < 0 else "",
                str(t.meta.get("network") or "") if m.asset_kind == AssetKind.EXCHANGE and m.amount < 0 else "",
            ])
            n += 1
    return n


def print_summary(rows: list[Classified], unknown: dict[str, int], names: dict[str, str], top: int = 15) -> None:
    by_wallet: dict[str, Counter[str]] = {}
    for r in rows:
        key = r.classification.value + (f"/{r.subtype}" if r.subtype else "")
        by_wallet.setdefault(r.movement.wallet, Counter())[key] += 1
    for w, c in by_wallet.items():
        print(f"\n{w}  ({sum(c.values())} rows)")
        for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {n:5}  {k}")
    low = [r for r in rows if r.confidence < 1.0]
    print(f"\nrows with confidence < 1.0: {len(low)}")
    if unknown:
        print(f"\nunregistered contracts encountered (top {top}):")
        for a, n in sorted(unknown.items(), key=lambda kv: -kv[1])[:top]:
            print(f"  {a}  {n:4}  {names.get(a, '')}")


def write_positions_csv(path: Path, positions: list[Position]) -> None:
    rows = [p.summary() for p in positions]
    with path.open("w", newline="") as f:
        if not rows:
            f.write("position\n")
            return
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        for r in rows:
            r = dict(r)
            r["opened"] = datetime.fromtimestamp(int(r["opened"]), UTC).strftime("%Y-%m-%d")
            wr.writerow(r)


def _d(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _short(a: str) -> str:
    return f"{a[:10]}…{a[-4:]}" if len(a) > 16 else a


def write_summary(path: Path, rows: list[Classified], positions: list[Position], unknown: dict[str, int],
                  names: dict[str, str], labels: dict[str, str], price_stats: dict[str, int],
                  low_conf_limit: int = 40, matches: list[str] | None = None,
                  exchange_checks: list[str] | None = None, exchange_links: list[str] | None = None) -> None:
    L: list[str] = []
    w = L.append
    w("# Transaction classification summary\n")
    span = (min(r.movement.timestamp for r in rows), max(r.movement.timestamp for r in rows)) if rows else (0, 0)
    w(f"{len(rows)} classified rows, {len({r.movement.wallet for r in rows})} wallets, "
      f"{_d(span[0])} → {_d(span[1])}.\n")

    w("## Wallets\n")
    w("| chain | wallet | label | rows | txs | gas (native) | gas USD | net USD of non-spam flows |")
    w("|---|---|---|---:|---:|---:|---:|---:|")
    by_w: dict[tuple[str, str], list[Classified]] = defaultdict(list)
    for r in rows:
        by_w[(r.movement.chain, r.movement.wallet)].append(r)
    for (ch, wl), rs in sorted(by_w.items()):
        gas = sum((r.gas_fee_native or Decimal(0) for r in rs), Decimal(0))
        gas_usd = sum((r.gas_fee_usd or Decimal(0) for r in rs), Decimal(0))
        flow = sum((r.usd_value or Decimal(0) for r in rs if not r.subtype.startswith("spam_")), Decimal(0))
        w(f"| {ch} | `{_short(wl)}` | {wallet_label(labels, ch, wl)} | {len(rs)} | {len({r.tx.tx_hash for r in rs})} | "
          f"{gas:.4f} | {gas_usd:.2f} | {flow:,.2f} |")

    w("\n## Classes (rows)\n")
    w("| chain | classification | subtype | rows | txs |")
    w("|---|---|---|---:|---:|")
    cnt: Counter[tuple[str, str, str]] = Counter()
    txs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in rows:
        k = (r.movement.chain, r.classification.value, r.subtype.split(":")[0])
        cnt[k] += 1
        txs[k].add(r.tx.tx_hash)
    for k, n in sorted(cnt.items()):
        w(f"| {k[0]} | {k[1]} | {k[2]} | {n} | {len(txs[k])} |")

    w("\n## Orders\n")
    orders: dict[str, list[Classified]] = defaultdict(list)
    for r in rows:
        if r.order_id:
            orders[r.order_id].append(r)
    oc: Counter[tuple[str, str, str]] = Counter()
    for oid, rs in orders.items():
        placed = [r for r in rs if r.classification == Cls.ORDER_PLACED]
        fill = [r for r in rs if r.classification == Cls.ORDER_FILLED]
        ref = placed[0] if placed else (fill[0] if fill else rs[0])
        status = ref.subtype.split(":", 1)[1] if ":" in ref.subtype else ref.subtype
        oc[(ref.movement.chain, ref.protocol.split(" (")[0], status)] += 1
    w("| chain | venue | outcome | orders |")
    w("|---|---|---|---:|")
    for k, n in sorted(oc.items()):
        w(f"| {k[0]} | {k[1]} | {k[2]} | {n} |")
    unresolved = [(oid, rs) for oid, rs in orders.items()
                  if any(x in rs[0].subtype for x in ("open", "spent_outside", "rest_unfilled"))]
    if unresolved:
        w("\nUnresolved orders (open, expired remainder, or settled outside the wallets' history):\n")
        for oid, rs in sorted(unresolved, key=lambda x: x[1][0].movement.timestamp):
            r0 = rs[0]
            w(f"- {_d(r0.movement.timestamp)} `{oid[:40]}` {r0.protocol} — {r0.subtype} — {r0.order_details[:140]}")

    if exchange_checks or exchange_links:
        w("\n## Exchange ledgers\n")
        for c in exchange_checks or []:
            w(f"- {c}")
        if exchange_links:
            w(f"\n{len(exchange_links)} exchange deposits/withdrawals linked to wallets or other exchanges "
              "(tx hash = exact; otherwise amount/time/address evidence):\n")
            for line in exchange_links:
                w(f"- {line}")

    w("\n## Heuristic matches\n")
    w("Pairs linked by value/time/counterparty evidence rather than an exact on-chain link "
      "(confidence < 1.0; weights in settings.yaml `matching`).\n")
    for d in matches or []:
        w(f"- {d}")
    if not matches:
        w("None.")

    w("\n## LP and yield positions\n")
    if positions:
        w("| chain | wallet | venue | position | status | deposited | withdrawn | cost USD | withdrawn USD | realized P&L USD |")
        w("|---|---|---|---|---|---|---|---:|---:|---:|")
        for p in positions:
            s = p.summary()
            w(f"| {s['chain']} | `{_short(s['wallet'])}` | {s['venue']} | {s['lp_token']} | {s['status']} | {s['deposited']} | "
              f"{s['withdrawn']} | {s['cost_basis_usd']} | {s['withdrawn_usd']} | {s['realized_pnl_usd']} |")
        w("\nCost basis includes batcher/DEX fees paid with the order; open positions have no realized P&L.")
    else:
        w("None.")

    w("\n## Rows to review (confidence < 1.0)\n")
    low = [r for r in rows if r.confidence < 1.0]
    lc: Counter[tuple[str, str, str]] = Counter((r.movement.chain, r.classification.value, r.subtype.split(":")[0]) for r in low)
    w("| chain | class | subtype | rows |")
    w("|---|---|---|---:|")
    for k, n in lc.most_common():
        w(f"| {k[0]} | {k[1]} | {k[2]} | {n} |")
    non_spam = [r for r in low if not r.subtype.startswith("spam_")][:low_conf_limit]
    if non_spam:
        w("\nNon-spam examples:\n")
        for r in non_spam:
            w(f"- {_d(r.movement.timestamp)} `{r.tx.tx_hash[:16]}` {r.classification.value}/{r.subtype} "
              f"{r.movement.amount.normalize():f} {r.movement.symbol} ({r.confidence:.2f}) — {r.reason[:120]}")

    w("\n## Unregistered contracts / scripts\n")
    w("Add these to `src/loledger/registry/*.yaml` once identified.\n")
    w("| address / script | rows | known name |")
    w("|---|---:|---|")
    for a, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
        w(f"| `{a}` | {n} | {names.get(a, '')} |")

    w("\n## Prices\n")
    w(", ".join(f"{k}: {v}" for k, v in sorted(price_stats.items())) or "not run")
    unpriced = Counter((r.movement.chain, r.movement.symbol) for r in rows
                       if r.usd_value is None and r.movement.amount != 0 and not r.subtype.startswith("spam_"))
    if unpriced:
        w("\n\nUnpriced (no market price found): " + ", ".join(f"{s} ({c}, {n})" for (c, s), n in unpriced.most_common(20)))
    w("")
    path.write_text("\n".join(L))
