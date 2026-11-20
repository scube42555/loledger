"""CLI entry point.

  classify fetch   --addresses addresses.csv          # fetch + cache + normalize (phase 1)
  classify verify  --addresses addresses.csv          # computed vs live on-chain balances
  classify movements --out movements.csv              # dump the normalized movements table
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters.evm.fetch import EvmFetcher
from .adapters.evm.normalize import balances
from .adapters.cardano.fetch import CardanoFetcher
from .adapters.cardano.normalize import current_epoch as cardano_current_epoch
from .adapters.cardano.normalize import normalize as normalize_cardano
from .adapters.cardano.normalize import reward_account, reward_account_movements
from .adapters.evm.pipeline import load_wallet
from .config import ConfigError, Settings, WalletSpec, load_settings, load_wallets, split_gifts
from .http import HttpClient
from .model import AssetKind, Movement, TxContext, scale
from .decode.evm_orders import annotate_gelato, annotate_kyber
from .registry import load_registry
from .report import print_summary, write_csv
from .rules import Classified, Cls, RuleContext, classify_wallet, dedupe_gas, link_same_tx_internal
from .store import Store

log = logging.getLogger("txclass")


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def cmd_fetch(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    wallets, _ = split_gifts(load_wallets(Path(args.addresses), settings, args.chain))
    wallet_set = {w.address for w in wallets}
    by_chain: dict[str, list[WalletSpec]] = defaultdict(list)
    for w in wallets:
        by_chain[w.chain.name].append(w)
    for chain_name, ws in by_chain.items():
        chain = ws[0].chain
        if chain.kind == "cardano":
            _fetch_cardano(args, chain, ws, store)
            continue
        if chain.kind == "utxo":
            _fetch_utxo(chain, ws, store)
            continue
        if chain.kind == "solana":
            _fetch_solana(chain, ws, store)
            continue
        if chain.kind == "sui":
            _fetch_sui(args, chain, ws, store)
            continue
        if chain.kind == "algorand":
            _fetch_algorand(args, chain, ws, store)
            continue
        if chain.kind == "injective":
            _fetch_injective(chain, ws, store)
            continue
        if chain.kind == "hedera":
            _fetch_hedera(chain, ws, store)
            continue
        if chain.kind == "chia":
            _fetch_chia(chain, ws, store)
            continue
        if chain.kind != "evm":
            log.warning("skipping %d %s wallet(s): adapter not implemented yet", len(ws), chain_name)
            continue
        try:
            fetcher = EvmFetcher(chain, store, refresh_tail=args.refresh)
        except ConfigError as e:
            log.warning("skipping %d %s wallet(s): %s", len(ws), chain_name, e)
            continue
        touched: Counter[str] = Counter()
        print(f"\n== {chain_name} (chain id {chain.chain_id}, explorer {chain.explorer})")
        if args.cmd == "run":
            print("  (wallet summaries below come from cache unless new data was fetched)")
        for w in ws:
            log.info("fetching %s", w.address)
            try:
                bundle, moves, txs = load_wallet(fetcher, w.address)
            except Exception as e:  # one bad endpoint must not abort the other chains
                log.error("  %s %s: fetch failed, skipped this run (%s: %s)", chain_name, w.address,
                          type(e).__name__, str(e)[:160])
                continue
            store.replace_wallet_movements(chain.name, w.address, moves)
            store.upsert_txs(txs.values())
            for m in moves:
                touched[m.counterparty] += 1
            for t in txs.values():
                if t.tx_from == w.address and t.tx_to:
                    touched[t.tx_to] += 1
            _print_wallet_summary(w, bundle.rows, moves, txs)
        for a in wallet_set:
            touched.pop(a, None)
        log.info("checking %d counterparties (contract vs EOA, verified names)", len(touched))
        fetcher.enrich_addresses(touched, names=not args.no_names)
        if args.top:
            _print_touched(chain_name, touched, store.get_address_info(chain_name), args.top)
    _fetch_exchanges(settings, store)
    args.exchange_checks = _load_file_exchanges(settings, store)
    total_calls = store.stats.misses
    print(f"\ncache: {store.stats.hits} hits, {total_calls} misses (network fetches)")
    return 0


EXCHANGE_WALLET = {"Crypto.com": "cryptocom:exchange"}
EXCHANGE_WALLETS: dict[str, set[str]] = {"Crypto.com": {"cryptocom:exchange"}}


def _fetch_exchanges(settings: Settings, store: Store) -> None:
    """Exchange accounts with credentials in .env are read automatically (not listed in addresses.csv)."""
    from .adapters.exchange import cryptocom

    chain = settings.chains.get("Crypto.com")
    if chain is None:
        return
    try:
        client = cryptocom.CryptoComClient(store)
    except ConfigError as e:
        log.info("Crypto.com skipped: %s", e)
        return
    print(f"\n== Crypto.com Exchange (API, since {chain.history_start})")
    bundle = client.fetch(chain.history_start)
    moves, txs = cryptocom.normalize(bundle, chain.name, EXCHANGE_WALLET[chain.name])
    store.replace_wallet_movements(chain.name, EXCHANGE_WALLET[chain.name], moves)
    store.upsert_txs(txs.values())
    kinds = Counter(str(t.meta.get("journal_type")) for t in txs.values())
    print(f"  journal {len(bundle.journal)} entries, {len(bundle.trades)} trades, {len(bundle.deposits)} deposits, "
          f"{len(bundle.withdrawals)} withdrawals -> {len(moves)} movements ({dict(kinds)})")


def _load_file_exchanges(settings: Settings, store: Store) -> list[str]:
    """Parse file-based exchange exports into the store. Returns self-check lines for the summary."""
    from .adapters.exchange import files

    checks: list[str] = []
    for chain in settings.chains.values():
        if chain.kind != "exchange" or not chain.parser:
            continue
        path = Path(chain.export)
        if not path.exists():
            log.info("%s skipped: %s not found", chain.name, path)
            continue
        parsers = {"mexc": lambda: files.parse_mexc(path, chain.tz_hours, chain.name),
                   "gemini": lambda: files.parse_gemini(path, chain.name),
                   "okx": lambda: files.parse_okx(path, chain.name),
                   "binanceus": lambda: files.parse_binanceus(path, chain.name),
                   "coinbase_manual": lambda: files.parse_coinbase_manual(path, chain.name)}
        if chain.parser not in parsers:
            log.warning("%s: unknown parser %r", chain.name, chain.parser)
            continue
        ledger = parsers[chain.parser]()
        by_wallet: dict[str, list[Movement]] = defaultdict(list)
        for m in ledger.moves:
            by_wallet[m.wallet].append(m)
        for wid, ms in by_wallet.items():
            store.replace_wallet_movements(chain.name, wid, ms)
            EXCHANGE_WALLETS.setdefault(chain.name, set()).add(wid)
        store.upsert_txs(ledger.txs.values())
        print(f"\n== {chain.name} (export {path}): {len(ledger.moves)} movements in {len(ledger.txs)} entries")
        for c in ledger.checks:
            print(f"  {c}")
        checks.extend(ledger.checks)
    return [c if "MEXC futures" not in c else c + " (user recalls losing these funds to futures trading losses; "
            "the export has no matching loss/liquidation entries, so the residual is reported, not booked)"
            for c in checks]


def solana_order_status(rows: list[Classified], client: Any, reg: Any) -> list[str]:
    """Unresolved Solana order placements: is the order account (owned by the order program) still on-chain?
    Live state, so never cached."""
    notes: list[str] = []
    for r in rows:
        prog = r.tx.tx_to
        accounts = (r.tx.meta.get("ix_accounts") or {}).get(prog, [])
        live = None
        for a in accounts[:6]:
            info = (client.rpc("getAccountInfo", [a, {"encoding": "base64"}]) or {}).get("value")
            if info and info.get("owner") == prog:
                live = a
                break
        if live:
            r.subtype = "escrow_open"
            r.reason += f"; order account {live[:10]}… still exists on-chain (order open — cancel to recover the escrow)"
        else:
            r.subtype = "escrow_closed_outside_history"
            r.reason += "; order account no longer exists (filled or cancelled; settlement not in this wallet's history)"
        notes.append(f"Solana {reg.contracts.get(prog).name if reg.contracts.get(prog) else prog[:10]} order "
                     f"{r.tx.tx_hash[:12]}… {abs(r.movement.amount).normalize():f} {r.movement.symbol}: {r.subtype}")
    return notes


def _fetch_chia(chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.chia.fetch import ChiaClient, address_to_puzzle_hash
    from .adapters.chia.normalize import normalize as normalize_chia
    from .adapters.chia.normalize import pair_bundles

    client = ChiaClient(chain, store)
    print(f"\n== {chain.name} (coinset.org)")
    results: dict[str, tuple[list[Movement], dict[str, TxContext]]] = {}
    raw: dict[str, list[dict[str, Any]]] = {}
    for w in ws:
        try:
            ph = address_to_puzzle_hash(w.address)
            raw[w.address] = client.coin_records(ph)
            results[w.address] = normalize_chia(chain, w.address, ph, raw[w.address], client)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
    for note in pair_bundles(results):
        print(f"  pooled spend: {note}")
    for w in ws:
        if w.address not in results:
            continue
        moves, txs = results[w.address]
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"coins": raw[w.address]}, moves, txs)


def _fetch_hedera(chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.hedera.fetch import HederaClient
    from .adapters.hedera.normalize import normalize as normalize_hedera

    client = HederaClient(chain, store)
    print(f"\n== {chain.name} (mirror node)")
    for w in ws:
        try:
            hist = client.history(w.address)
            moves, txs = normalize_hedera(chain, w.address, hist, client.token)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
            continue
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist}, moves, txs)


def _injective_denom_meta(store: Store, client: Any) -> Any:
    import httpx as _httpx

    def meta(denom: str) -> tuple[str, int]:
        c = store.get_raw("injective-lcd", "denom_meta", {"d": denom})
        if c is not None:
            return str(c[0]), int(c[1])
        sym, dec = denom[:12], 0
        try:
            if denom.startswith("peggy0x"):
                info = _httpx.get(f"https://coins.llama.fi/prices/current/ethereum:{denom[5:]}", timeout=20).json()
                coin = (info.get("coins") or {}).get(f"ethereum:{denom[5:]}") or {}
                sym, dec = str(coin.get("symbol") or denom[5:13]), int(coin.get("decimals") or 18)
            elif denom.startswith("ibc/"):
                tr = client.get(f"https://sentry.lcd.injective.network/ibc/apps/transfer/v1/denom_traces/{denom[4:]}")
                base = str((tr.get("denom_trace") or {}).get("base_denom") or denom)
                sym = base[1:].upper() if base[:1] in "ua" else base.upper()
                dec = 6 if base.startswith("u") else (18 if base.startswith("a") else 0)
            elif denom.startswith("factory/"):
                sym, dec = denom.rsplit("/", 1)[-1][:12], 6
        except Exception as e:  # metadata is cosmetic: keep going with a raw denom
            log.warning("  injective denom %s: metadata lookup failed (%s)", denom[:20], e)
        store.put_raw("injective-lcd", "denom_meta", {"d": denom}, [sym, dec])
        return sym, dec

    return meta


def _fetch_injective(chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.injective.fetch import InjectiveClient
    from .adapters.injective.normalize import normalize as normalize_inj

    client = InjectiveClient(chain, store)
    print(f"\n== {chain.name} (explorer API)")
    modules = client.module_accounts()
    meta = _injective_denom_meta(store, client)
    for w in ws:
        try:
            hist = client.history(w.address)
            moves, txs = normalize_inj(chain, w.address, hist, modules, meta)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
            continue
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist}, moves, txs)


def _fetch_algorand(args: argparse.Namespace, chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.algorand.fetch import AlgorandClient
    from .adapters.algorand.normalize import normalize as normalize_algo

    client = AlgorandClient(chain, store, refresh_tail=args.refresh)
    print(f"\n== {chain.name} (Algonode indexer)")
    for w in ws:
        try:
            hist = client.history(w.address)
            moves, txs = normalize_algo(chain, w.address, hist, client.asset)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
            continue
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist}, moves, txs)


def _fetch_sui(args: argparse.Namespace, chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.sui.fetch import SuiClient
    from .adapters.sui.normalize import normalize as normalize_sui

    client = SuiClient(chain, store, refresh_tail=args.refresh)
    print(f"\n== {chain.name} (public GraphQL)")
    for w in ws:
        try:
            hist = client.history(w.address)
            moves, txs = normalize_sui(chain, w.address, hist, client.coin_metadata)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
            continue
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist}, moves, txs)


def _fetch_solana(chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.solana.fetch import SolanaClient, resolve_symbols, stake_accounts
    from .adapters.solana.normalize import normalize as normalize_solana
    from .adapters.solana.normalize import stake_reward_movements

    client = SolanaClient(chain, store)
    print(f"\n== {chain.name} (public RPC)")
    hist: dict[str, list[dict[str, Any]]] = {}
    for w in ws:
        try:
            hist[w.address] = client.history(w.address)
        except Exception as e:
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
    mints = {b["mint"] for h in hist.values() for tx in h
             for b in ((tx.get("meta") or {}).get("postTokenBalances") or []) + ((tx.get("meta") or {}).get("preTokenBalances") or [])}
    # stake accounts controlled by the listed wallets become wallets of their own (history + epoch rewards)
    listed = {w.address for w in ws}
    stake: list[WalletSpec] = []
    stake_set: set[str] = set()  # every stake account, whether discovered or listed in addresses.csv
    for w in list(ws):
        accts = stake_accounts(w.address, hist.get(w.address, []))
        store.put_raw("txclass", "solana_stake_accounts", {"wallet": w.address}, accts)
        stake_set.update(accts)
        for a in accts:
            if a not in listed:
                listed.add(a)
                stake.append(WalletSpec(a, chain, f"{w.label} · stake {a[:6]}"))
    for w in stake:
        try:
            hist[w.address] = client.history(w.address)
        except Exception as e:
            log.error("  %s %s: stake account fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
    rewards = _solana_stake_rewards(client, {a: hist.get(a, []) for a in stake_set})
    mints = {b["mint"] for h in hist.values() for tx in h
             for b in ((tx.get("meta") or {}).get("postTokenBalances") or []) + ((tx.get("meta") or {}).get("preTokenBalances") or [])}
    symbols = resolve_symbols(store, mints)
    for w in [*ws, *stake]:
        if w.address not in hist:
            continue
        moves, txs = normalize_solana(chain, w.address, hist[w.address], symbols)
        if w.address in rewards:
            rm, rt = stake_reward_movements(chain, w.address, rewards[w.address])
            moves += rm
            txs.update(rt)
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist[w.address]}, moves, txs)
        if w.address in rewards:
            print(f"    {len(rewards[w.address])} epoch rewards, "
                  f"{sum(r[1] for r in rewards[w.address]) / 1e9:.6f} {chain.native_symbol}")


def _solana_stake_rewards(client: Any, hist: dict[str, list[dict[str, Any]]]) -> dict[str, list[tuple[int, int, int]]]:
    """Per stake account: (epoch, lamports, block time) for every epoch between its first and last activity."""
    if not hist:
        return {}
    now = client.current_epoch()
    span: dict[str, tuple[int, int]] = {}
    for a, h in hist.items():
        slots = [int(t.get("slot") or 0) for t in h]
        if not slots:
            continue
        closed = client.balance(a) == 0
        span[a] = (min(slots) // 432000, (max(slots) // 432000) if closed else now - 1)
    out: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    if not span:
        return out
    lo, hi = min(s[0] for s in span.values()), max(s[1] for s in span.values())
    print(f"  stake rewards: {len(span)} stake account(s), epochs {lo}–{hi}")
    for epoch in range(lo, hi + 1):
        accts = sorted(a for a, (s0, s1) in span.items() if s0 <= epoch <= s1)
        if not accts:
            continue
        try:
            res = client.inflation_rewards(accts, epoch, final=epoch < now - 1)
        except RuntimeError as e:
            if "rewards period still active" not in str(e):
                raise
            log.info("  epoch %d rewards still being paid out; picked up on the next run", epoch)
            continue
        for a, r in zip(accts, res):
            if r and int(r.get("amount") or 0):
                out[a].append((epoch, int(r["amount"]), client.block_time(int(r["effectiveSlot"]))))
    return out


def expand_solana_stake(wallets: list[WalletSpec], store: Store) -> list[WalletSpec]:
    """Add the stake accounts discovered during fetch, so classification and verification include them."""
    have = {(w.chain.name, w.address) for w in wallets}
    extra: list[WalletSpec] = []
    for w in wallets:
        if w.chain.kind != "solana":
            continue
        for a in store.get_raw("txclass", "solana_stake_accounts", {"wallet": w.address}) or []:
            if (w.chain.name, a) not in have:
                have.add((w.chain.name, a))
                extra.append(WalletSpec(a, w.chain, f"{w.label} · stake {a[:6]}"))
    return [*wallets, *extra]


def _fetch_utxo(chain: Any, ws: list[WalletSpec], store: Store) -> None:
    from .adapters.utxo.fetch import UtxoClient
    from .adapters.utxo.normalize import normalize as normalize_utxo

    client = UtxoClient(chain, store)
    print(f"\n== {chain.name} ({chain.explorer})")
    for w in ws:
        try:
            hist = client.history(w.address)
        except Exception as e:  # keyless public APIs: one failure must not stop the run
            log.error("  %s %s: fetch failed (%s: %s)", chain.name, w.address, type(e).__name__, str(e)[:160])
            continue
        moves, txs = normalize_utxo(chain, w.address, hist)
        store.replace_wallet_movements(chain.name, w.address, moves)
        store.upsert_txs(txs.values())
        _print_wallet_summary(w, {"txs": hist}, moves, txs)


def cardano_wallet_ids(chain: Any, ws: list[WalletSpec], store: Store) -> dict[str, str]:
    """csv address -> wallet id (stake address)."""
    return CardanoFetcher(chain, store).resolve([w.address for w in ws])


def _fetch_cardano(args: argparse.Namespace, chain: Any, ws: list[WalletSpec], store: Store) -> None:
    fetcher = CardanoFetcher(chain, store, refresh_tail=args.refresh)
    ids = fetcher.resolve([w.address for w in ws])
    wallets = set(ids.values())
    print(f"\n== {chain.name} (Koios; {len(ws)} csv rows -> {len(wallets)} stake keys)")
    for w in ws:
        if ids[w.address] != w.address:
            print(f"  {w.address[:20]}… -> {ids[w.address]}")
    for stake in sorted(wallets):
        bundle = fetcher.fetch_wallet(stake)
        moves, txs = normalize_cardano(chain, bundle, wallets)
        store.replace_wallet_movements(chain.name, stake, moves)
        store.upsert_txs(txs.values())
        spec = WalletSpec(address=stake, chain=chain, label="")
        _print_wallet_summary(spec, {"txs": list(bundle.txs.values())}, moves, txs)
        rmoves, rtxs = reward_account_movements(chain, stake, fetcher.koios.reward_history(stake), txs,
                                                cardano_current_epoch(time.time()))
        store.replace_wallet_movements(chain.name, reward_account(stake), rmoves)
        store.upsert_txs(rtxs.values())
        earned = sum((m.amount for m in rmoves if m.amount > 0), Decimal(0))
        print(f"  reward account: {len(rtxs)} epoch rewards ({earned:.6f} ADA), "
              f"{len(rmoves) - len(rtxs)} withdrawal(s)")


def _print_wallet_summary(w: WalletSpec, rows: dict[str, list[dict[str, Any]]], moves: list[Movement],
                          txs: dict[str, TxContext]) -> None:
    sent = [t for t in txs.values() if t.tx_from == w.address]
    failed = sum(1 for t in sent if t.is_error)
    gas = sum((t.gas_fee for t in sent), Decimal(0))
    span = f"{_date(min(t.timestamp for t in txs.values()))} → {_date(max(t.timestamp for t in txs.values()))}" if txs else "no activity"
    raw = ", ".join(f"{k}={len(v)}" for k, v in rows.items())
    kinds = Counter(m.asset_kind.value for m in moves)
    print(f"\n{w.address}{' (' + w.label + ')' if w.label else ''}")
    print(f"  span {span} | txs touched {len(txs)} | sent {len(sent)} (failed {failed}) | gas {gas:.4f}")
    print(f"  raw rows: {raw}")
    print(f"  movements {len(moves)}: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


def _print_touched(chain: str, touched: Counter[str], info: dict[str, tuple[bool | None, str]], top: int) -> None:
    print(f"\n== most-used counterparties on {chain} (top {top})")
    print(f"  {'address':42}  {'kind':8}  {'n':>5}  name")
    for a, n in touched.most_common(top):
        is_c, name = info.get(a, (None, ""))
        kind = "contract" if is_c else ("EOA" if is_c is False else "?")
        print(f"  {a:42}  {kind:8}  {n:>5}  {name}")


def cmd_verify(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    wallets = expand_solana_stake(split_gifts(load_wallets(Path(args.addresses), settings, args.chain))[0], store)
    bad = 0
    cardano = [w for w in wallets if w.chain.kind == "cardano"]
    if cardano:
        bad += _safe_verify(lambda: _verify_cardano(cardano, store))
    bad += _safe_verify(lambda: _verify_utxo([w for w in wallets if w.chain.kind == "utxo"], store))
    bad += _safe_verify(lambda: _verify_solana([w for w in wallets if w.chain.kind == "solana"], store))
    bad += _safe_verify(lambda: _verify_sui([w for w in wallets if w.chain.kind == "sui"], store))
    bad += _safe_verify(lambda: _verify_algorand([w for w in wallets if w.chain.kind == "algorand"], store))
    bad += _safe_verify(lambda: _verify_injective([w for w in wallets if w.chain.kind == "injective"], store))
    bad += _safe_verify(lambda: _verify_hedera([w for w in wallets if w.chain.kind == "hedera"], store))
    bad += _safe_verify(lambda: _verify_chia([w for w in wallets if w.chain.kind == "chia"], store))
    for w in wallets:
        if w.chain.kind != "evm" or not w.chain.rpc_url:
            continue
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        wallet_txs = {m.tx_hash for m in moves} | {h for h, t in txs.items() if t.tx_from == w.address}
        computed = balances(moves, {h: txs[h] for h in wallet_txs if h in txs}, w.address)
        meta = {m.asset_id: (m.symbol, m.decimals) for m in moves if m.asset_kind in (AssetKind.ERC20, AssetKind.NATIVE)}
        meta["native"] = (w.chain.native_symbol, w.chain.native_decimals)
        assets = [a for a in meta if a == "native" or computed.get(a, Decimal(0)) != 0 or args.all]
        live = _live_balances(w, assets, meta)
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        print(f"  {'asset':10} {'contract':13} {'computed':>24} {'on-chain':>24} {'diff':>16}")
        for a in assets:
            c = computed.get(a, Decimal(0))
            o = live.get(a)
            diff = None if o is None else c - o
            tol = max(Decimal(1).scaleb(-meta[a][1]), Decimal("1e-12") * abs(o if o is not None else Decimal(0)))
            ok = diff is not None and abs(diff) <= tol
            bad += 0 if ok else 1
            flag = "" if ok else "  <-- MISMATCH"
            sym = meta[a][0][:10]
            print(f"  {sym:10} {_short(a) if a != 'native' else 'native':13} {c:>24.8f} {('n/a' if o is None else f'{o:.8f}'):>24} "
                  f"{('' if diff is None else f'{diff:.8f}'):>16}{flag}")
    bad += _safe_verify(lambda: _verify_exchanges(settings, store))
    snap = store.get_raw("verify", "live_balances", {}) or {"wallets": {}}
    snap["wallets"].update(_LIVE)
    if not args.chain:
        snap["unlisted_evm"] = _scan_unlisted_evm(wallets, settings)
    snap["ts"] = int(time.time())
    store.put_raw("verify", "live_balances", {}, snap)
    print(f"\n{bad} mismatching balance(s); live snapshot of {len(_LIVE)} wallet(s) saved for `classify reconcile`")
    return 0


_LIVE: dict[str, dict[str, str]] = {}


def _scan_unlisted_evm(wallets: list[WalletSpec], settings: Settings) -> dict[str, dict[str, str]]:
    """Each listed EVM address on every configured EVM network it is *not* listed for: native balance
    and sent-tx count. Bridges credit the same address on the destination network."""
    listed = {(w.chain.name, w.address) for w in wallets}
    labels = {w.address: w.label for w in wallets if w.chain.kind == "evm"}
    out: dict[str, dict[str, str]] = {}
    for chain in (c for c in settings.chains.values() if c.kind == "evm" and c.rpc_url):
        http = HttpClient(chain.rpc_rps)
        assert chain.rpc_url
        for a in sorted(labels):
            if (chain.name, a) in listed:
                continue
            try:
                body = http.request_json("POST", chain.rpc_url, json_body=[
                    {"jsonrpc": "2.0", "id": 0, "method": "eth_getBalance", "params": [a, "latest"]},
                    {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount", "params": [a, "latest"]}])
            except Exception as e:
                print(f"  unlisted-network scan: {chain.name} {a[:10]}… failed ({type(e).__name__})")
                continue
            res = {r.get("id"): r.get("result") for r in body} if isinstance(body, list) else {}
            bal, nonce = (int(res[i], 16) if isinstance(res.get(i), str) and res[i] != "0x" else 0 for i in (0, 1))
            if bal or nonce:
                out[f"{chain.name}|{a}"] = {"chain": chain.name, "address": a, "label": labels[a],
                                            "native": f"{scale(bal, chain.native_decimals):f} {chain.native_symbol}",
                                            "nonce": str(nonce)}
                print(f"  activity on an unlisted network: {chain.name} {a} ({labels[a]}): "
                      f"{scale(bal, chain.native_decimals)} {chain.native_symbol}, {nonce} tx(s) sent")
    return out


def _record(chain: str, wallet: str, live: Mapping[str, Decimal]) -> None:
    """Remember live balances so `reconcile` can compare classified holdings without refetching."""
    _LIVE.setdefault(f"{chain}|{wallet}", {}).update({k: f"{v:f}" for k, v in live.items()})


def cmd_basis(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .costbasis import Engine, load as load_legs, write_reports

    legs = load_legs(Path(args.classified), Path(args.manual))
    res = Engine(pre=args.method_pre, post=args.method_post).run(legs)
    paths = write_reports(res, Path(args.out_dir))
    print(f"{len(legs)} legs -> {len(res.disposals)} disposals, {len(res.income)} income, {len(res.futures)} futures, "
          f"{len(res.flags)} flags")
    print(f"wrote {paths['summary']} (+ {', '.join(p.name for k, p in paths.items() if k != 'summary')})")
    return 0


def cmd_tax(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .costbasis import Engine, load as load_legs
    from .taxforms import Profile, export_year

    res = Engine(pre=args.method_pre, post=args.method_post).run(load_legs(Path(args.classified), Path(args.manual)))
    profile = Profile.load(Path(args.profile))
    for year in args.year:
        out = export_year(res, year, Path(args.out_dir) / str(year), profile, Path(args.classified))
        sd = out["sched_d"]
        print(f"{year}: {out['lines']} Form 8949 lines; Schedule D line 7 {sd['7']:,.2f}, line 15 {sd['15']:,.2f}, "
              f"line 16 {sd['16']:,.2f}; Schedule 1 digital-asset income {out['income']:,.2f}")
        for n in out["notes"]:
            print(f"  note: {n}")
        print(f"  wrote {len(out['files'])} files to {Path(args.out_dir) / str(year)}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .web.app import serve

    serve(Path("."), args.host, args.port, args.method_pre, args.method_post)
    return 0


def cmd_reconcile(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .reconcile import load as load_classified
    from .reconcile import report as reconcile_report

    snap = store.get_raw("verify", "live_balances", {}) or {}
    stats = reconcile_report(load_classified(Path(args.classified)), snap, Path(args.out), Path(args.holdings),
                             Decimal(str(args.min_usd)))
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    print(f"wrote {args.out} and {args.holdings}")
    return 0


def _verify_cardano(ws: list[WalletSpec], store: Store) -> int:
    chain = ws[0].chain
    fetcher = CardanoFetcher(chain, store)
    stakes = sorted(set(fetcher.resolve([w.address for w in ws]).values()))
    http = fetcher.koios.http
    url = fetcher.koios.url
    info = {a["stake_address"]: a for a in http.request_json("POST", f"{url}/account_info",
                                                               json_body={"_stake_addresses": stakes})}
    assets: dict[str, dict[str, int]] = defaultdict(dict)
    for a in http.request_json("POST", f"{url}/account_assets", json_body={"_stake_addresses": stakes}):
        assets[a["stake_address"]][f"{a['policy_id']}.{a['asset_name'] or ''}"] = int(a["quantity"])
    txs = store.load_txs(chain.name)
    bad = 0
    for st in stakes:
        moves = store.load_movements(chain.name, st)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        meta: dict[str, tuple[str, int]] = {"native": ("ADA", 6)}
        for m in moves:
            computed[m.asset_id] += m.amount
            meta[m.asset_id] = (m.symbol, m.decimals)
        for t in txs.values():
            if t.tx_from == st:  # includes fee-only txs (self-sends, delegation) with no movement
                computed["native"] -= t.gas_fee
        a = info.get(st, {})
        live: dict[str, Decimal] = {"native": scale(int(a.get("total_balance") or 0) - int(a.get("rewards_available") or 0), 6)}
        for k, q in assets[st].items():
            live[k] = scale(q, meta.get(k, ("", 0))[1])
        _record(chain.name, st, live)
        rmoves = store.load_movements(chain.name, reward_account(st))
        if rmoves:
            rc = sum((m.amount for m in rmoves), Decimal(0))
            rl = scale(int(a.get("rewards_available") or 0), 6)
            _record(chain.name, reward_account(st), {"native": rl})
            bad += 0 if rc == rl else 1
            print(f"\n{reward_account(st)}  ({len(rmoves)} movements)\n  {'ADA':12} {'native':13} {rc:>22f} {rl:>22f}"
                  f"{'' if rc == rl else '  <-- MISMATCH'}")
        print(f"\n{st}  ({len(moves)} movements; unclaimed rewards {scale(int(a.get('rewards_available') or 0), 6)} ADA)")
        shown = 0
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            if c == o and k != "native":
                continue
            ok = c == o
            bad += 0 if ok else 1
            sym = meta.get(k, (k[57:70], 0))[0][:12]
            print(f"  {sym:12} {k[:13]:13} {c:>22f} {o:>22f}{'' if ok else '  <-- MISMATCH'}")
            shown += 1
        n_ok = sum(1 for k in set(computed) | set(live) if k != "native" and computed.get(k, Decimal(0)) == live.get(k, Decimal(0)))
        print(f"  … {n_ok} token balances match exactly")
    return bad


def _safe_verify(fn: Any) -> int:
    """One provider being down must not abort the other chains' balance checks."""
    try:
        return int(fn())
    except Exception as e:
        print(f"\n  balance check failed for one chain ({type(e).__name__}: {str(e)[:120]}); counted as 1 mismatch")
        return 1


def _verify_utxo(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.utxo.fetch import UtxoClient

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed = sum((m.amount for m in moves), Decimal(0)) - sum(
            (t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        try:
            _, live_raw = UtxoClient(w.chain, store).stats(w.address)
        except Exception as e:
            print(f"\n{w.address}: live balance unavailable ({e})")
            continue
        live = scale(live_raw, w.chain.native_decimals)
        _record(w.chain.name, w.address, {"native": live})
        ok = computed == live
        bad += 0 if ok else 1
        print(f"\n{w.address}  ({len(moves)} movements)")
        print(f"  {w.chain.native_symbol:10} {'native':13} {computed:>24f} {live:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_solana(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.solana.fetch import SolanaClient

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        meta: dict[str, tuple[str, int]] = {"native": ("SOL", 9)}
        for m in moves:
            computed[m.asset_id] += m.amount
            meta[m.asset_id] = (m.symbol, m.decimals)
        computed["native"] -= sum((t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        client = SolanaClient(w.chain, store)
        live: dict[str, Decimal] = {"native": scale(client.balance(w.address), 9)}
        for mint, (raw, dec) in client.token_balances(w.address).items():
            live[mint] = scale(raw, dec)
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = c == o
            if ok and k != "native":
                continue
            bad += 0 if ok else 1
            print(f"  {meta.get(k, (k[:8], 0))[0][:10]:10} {k[:13]:13} {c:>24f} {o:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_sui(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.sui.fetch import SuiClient
    from .adapters.sui.normalize import SUI_TYPE

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        meta: dict[str, tuple[str, int]] = {"native": ("SUI", 9)}
        for m in moves:
            computed[m.asset_id] += m.amount
            meta[m.asset_id] = (m.symbol, m.decimals)
        computed["native"] -= sum((t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        client = SuiClient(w.chain, store)
        live: dict[str, Decimal] = {}
        for ct, raw in client.balances(w.address).items():
            key = "native" if ct == SUI_TYPE else ct
            live[key] = scale(raw, 9 if key == "native" else client.coin_metadata(ct)[1])
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = c == o
            if ok and k != "native":
                continue
            bad += 0 if ok else 1
            print(f"  {meta.get(k, (k.split('::')[-1], 0))[0][:10]:10} {k[-13:]:13} {c:>24f} {o:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_algorand(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.algorand.fetch import AlgorandClient

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        for m in moves:
            computed[m.asset_id] += m.amount
        computed["native"] -= sum((t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        client = AlgorandClient(w.chain, store)
        acct = client.account(w.address)
        live: dict[str, Decimal] = {"native": scale(int(acct.get("amount", 0)), 6)}
        for a in acct.get("assets") or []:
            live[str(a["asset-id"])] = scale(int(a["amount"]), client.asset(int(a["asset-id"]))[1])
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = c == o
            if ok and k != "native":
                continue
            bad += 0 if ok else 1
            print(f"  {'ALGO' if k == 'native' else k:10} {k[:13]:13} {c:>24f} {o:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_injective(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.injective.fetch import InjectiveClient

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        meta: dict[str, int] = {"native": 18}
        for m in moves:
            computed[m.asset_id] += m.amount
            meta[m.asset_id] = m.decimals
        computed["native"] -= sum((t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        live = {("native" if d == "inj" else d): scale(v, 18 if d == "inj" else meta.get(d, 0))
                for d, v in InjectiveClient(w.chain, store).balances(w.address).items()}
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = c == o
            if ok and k != "native":
                continue
            bad += 0 if ok else 1
            print(f"  {'INJ' if k == 'native' else k[:10]:10} {k[:13]:13} {c:>24f} {o:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_hedera(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.hedera.fetch import HederaClient

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed: dict[str, Decimal] = defaultdict(Decimal)
        meta: dict[str, int] = {}
        for m in moves:
            computed[m.asset_id] += m.amount
            meta[m.asset_id] = m.decimals
        computed["native"] -= sum((t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        acct = HederaClient(w.chain, store).account(w.address)
        live: dict[str, Decimal] = {"native": scale(int(acct["balance"]["balance"]), 8)}
        for tk in acct["balance"].get("tokens") or []:
            live[tk["token_id"]] = scale(int(tk["balance"]), meta.get(tk["token_id"], 0))
        _record(w.chain.name, w.address, live)
        print(f"\n{w.address}  ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live), key=lambda k: (k != "native", k)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = c == o
            if ok and k != "native":
                continue
            bad += 0 if ok else 1
            print(f"  {'HBAR' if k == 'native' else k:10} {k[:13]:13} {c:>24f} {o:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_chia(ws: list[WalletSpec], store: Store) -> int:
    from .adapters.chia.fetch import ChiaClient, address_to_puzzle_hash

    bad = 0
    for w in ws:
        moves = store.load_movements(w.chain.name, w.address)
        txs = store.load_txs(w.chain.name)
        computed = sum((m.amount for m in moves), Decimal(0)) - sum(
            (t.gas_fee for t in txs.values() if t.tx_from == w.address), Decimal(0))
        recs = ChiaClient(w.chain, store).coin_records(address_to_puzzle_hash(w.address))
        live = scale(sum(int(r["coin"]["amount"]) for r in recs if not r.get("spent")), 12)
        _record(w.chain.name, w.address, {"native": live})
        ok = computed == live
        bad += 0 if ok else 1
        print(f"\n{w.address}  ({len(moves)} movements)")
        print(f"  {'XCH':10} {'native':13} {computed:>24f} {live:>24f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _verify_exchanges(settings: Settings, store: Store) -> int:
    from .adapters.exchange import cryptocom

    bad = 0
    if "Crypto.com" in settings.chains:
        try:
            client = cryptocom.CryptoComClient(store)
        except ConfigError:
            return 0
        moves = store.load_movements("Crypto.com", EXCHANGE_WALLET["Crypto.com"])
        computed: dict[str, Decimal] = defaultdict(Decimal)
        for m in moves:
            computed[m.asset_id] += m.amount
        live = {p["instrument_name"]: Decimal(str(p["quantity"]))
                for p in client.call("private/user-balance", {})["data"][0]["position_balances"]}
        _record("Crypto.com", EXCHANGE_WALLET["Crypto.com"], live)
        print(f"\nCrypto.com Exchange ({len(moves)} movements)")
        for k in sorted(set(computed) | set(live)):
            c, o = computed.get(k, Decimal(0)), live.get(k, Decimal(0))
            ok = abs(c - o) < Decimal("1e-8")
            bad += 0 if ok else 1
            print(f"  {k:10} {'':13} {c:>24.8f} {o:>24.8f}{'' if ok else '  <-- MISMATCH'}")
    return bad


def _live_balances(w: WalletSpec, assets: list[str], meta: dict[str, tuple[str, int]]) -> dict[str, Decimal]:
    """Live (uncached) balances at 'latest' via JSON-RPC."""
    http = HttpClient(w.chain.rpc_rps)
    assert w.chain.rpc_url
    padded = w.address[2:].rjust(64, "0")
    out: dict[str, Decimal] = {}
    step = max(1, w.chain.rpc_batch)
    for i in range(0, len(assets), step):
        chunk = assets[i:i + step]
        payload = []
        for j, a in enumerate(chunk):
            if a == "native":
                payload.append({"jsonrpc": "2.0", "id": j, "method": "eth_getBalance", "params": [w.address, "latest"]})
            else:
                payload.append({"jsonrpc": "2.0", "id": j, "method": "eth_call",
                                "params": [{"to": a, "data": "0x70a08231" + padded}, "latest"]})
        body = http.request_json("POST", w.chain.rpc_url, json_body=payload)
        for r in body if isinstance(body, list) else []:
            res = r.get("result")
            if isinstance(res, str) and res.startswith("0x") and len(res) > 2:
                a = chunk[r["id"]]
                out[a] = scale(int(res[:66], 16), meta[a][1])
    return out


def cmd_movements(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    moves = store.load_movements(args.chain_name)
    txs: dict[str, TxContext] = {}
    for chain in {m.chain for m in moves}:
        txs.update(store.load_txs(chain))
    with open(args.out, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["chain", "timestamp", "date", "block", "tx_hash", "leg_id", "wallet", "direction",
                     "asset_id", "symbol", "amount", "counterparty", "source", "tx_from", "tx_to",
                     "method_id", "function_name", "is_error"])
        for m in moves:
            t = txs.get(m.tx_hash)
            wr.writerow([m.chain, m.timestamp, datetime.fromtimestamp(m.timestamp, UTC).isoformat(), m.block,
                         m.tx_hash, m.leg_id, m.wallet, m.direction.value, m.asset_id, m.symbol,
                         f"{m.amount:f}", m.counterparty, m.source, t.tx_from if t else "",
                         t.tx_to if t else "", t.method_id if t else "", t.function_name if t else "",
                         int(t.is_error) if t else ""])
    print(f"wrote {len(moves)} movements to {args.out}")
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    """Fetch (cache-first) + normalize + classify -> classified.csv."""
    cmd_fetch(args, settings, store)
    own, gifts = split_gifts(load_wallets(Path(args.addresses), settings, args.chain))
    wallets = expand_solana_stake(own, store)
    wallet_set = {w.address for w in wallets}
    labels: dict[str, str] = {}
    for w in wallets:  # per chain ("Ethereum|0x…"), plus the address's first label as a fallback for other chains
        labels[f"{w.chain.name}|{w.address}"] = w.label
        labels.setdefault(w.address, w.label)
    rows: list[Classified] = []
    unknown: dict[str, int] = {}
    names: dict[str, str] = {}
    for chain_name in dict.fromkeys(w.chain.name for w in wallets
                                    if w.chain.kind in ("evm", "utxo", "solana", "algorand", "injective", "hedera", "chia")):
        chain = settings.chains[chain_name]
        info = store.get_address_info(chain_name)
        ctx = RuleContext(chain=chain, wallets=wallet_set, registry=load_registry(chain_name, info))
        txs = store.load_txs(chain_name)
        chain_rows: list[Classified] = []
        assets: dict[str, tuple[str, int]] = {}
        for w in (w for w in wallets if w.chain.name == chain_name):
            moves = store.load_movements(chain_name, w.address)
            assets.update({m.asset_id: (m.symbol, m.decimals) for m in moves})
            chain_rows.extend(classify_wallet(w.address, moves, txs, ctx))
        annotate_kyber(chain_rows, ctx.registry)
        annotate_gelato(chain_rows, ctx.registry, assets)
        rows.extend(chain_rows)
        unknown.update(ctx.unknown_contracts)
        names.update({a: n for a, (_, n) in info.items() if n})
    sui_ws = [w for w in wallets if w.chain.kind == "sui"]
    if sui_ws:
        from .decode.sui_orders import classify_sui_wallet

        sui_chain = sui_ws[0].chain
        sctx = RuleContext(chain=sui_chain, wallets=wallet_set, registry=load_registry(sui_chain.name,
                                                                                         store.get_address_info(sui_chain.name)))
        stxs = store.load_txs(sui_chain.name)
        for w in sui_ws:
            rows.extend(classify_sui_wallet(w.address, store.load_movements(sui_chain.name, w.address), stxs, sctx))
    cardano_ws = [w for w in wallets if w.chain.kind == "cardano"]
    if cardano_ws:
        crows, unknown_scripts = _classify_cardano(cardano_ws, store)
        rows.extend(crows)
        for w in cardano_ws:  # label stake keys with the csv address they came from
            labels.setdefault(w.address, w.label)
        for sk, a in cardano_wallet_ids(cardano_ws[0].chain, cardano_ws, store).items():
            lab = next((w.label for w in cardano_ws if w.address == sk), "")
            if lab:
                labels.setdefault(a, lab)
            labels.setdefault(reward_account(a), f"{lab} · rewards" if lab else "reward account")
        unknown.update({f"cardano-script:{h}": n for h, n in unknown_scripts.items()})
    from .decode.exchanges import classify_exchange, link_exchange_transfers

    for chain_name, wallet_ids in EXCHANGE_WALLETS.items():
        xtxs = store.load_txs(chain_name)
        for wallet_id in sorted(wallet_ids):
            xmoves = store.load_movements(chain_name, wallet_id)
            if xmoves:
                rows.extend(classify_exchange(xmoves, xtxs, chain_name.lower(),
                                              credit_subtype=settings.chains[chain_name].credit_subtype))
                labels.setdefault(wallet_id, f"{chain_name} {wallet_id.split(':')[1]}")
    dedupe_gas(rows)
    link_same_tx_internal(rows)
    link_notes = link_exchange_transfers(rows)
    if link_notes:
        print(f"\nexchange transfers linked by tx hash: {len(link_notes)}")
        for line in link_notes:
            print(f"  {line}")
    from .decode.exchanges import link_exchange_heuristic

    dep_addrs = {c.name.lower(): set(c.deposit_addresses) for c in settings.chains.values() if c.deposit_addresses}
    hnotes = link_exchange_heuristic(rows, dep_addrs, labels)
    if hnotes:
        print(f"\nexchange transfers linked by amount/time/address (no tx hash available): {len(hnotes)}")
        for line in hnotes:
            print(f"  {line}")
    stats: dict[str, int] = {}
    if not args.no_prices:
        from .enrich import apply_prices
        from .prices import PriceService

        from .decode.cardano_orders import load_cardano_price_aliases

        aliases = {c: (load_cardano_price_aliases() if settings.chains[c].kind == "cardano" else load_registry(c).price_aliases)
                   for c in {r.movement.chain for r in rows}}
        stats = apply_prices(rows, settings.chains, PriceService(store), aliases)
        print(f"\nprices: {stats}")
    sol_rows = [r for r in rows if r.movement.chain == "Solana" and r.classification == Cls.ORDER_PLACED
                and r.subtype in ("escrow_open_or_unknown", "escrow")]
    if sol_rows:
        from .adapters.solana.fetch import SolanaClient

        for line in solana_order_status(sol_rows, SolanaClient(settings.chains["Solana"], store),
                                        load_registry("Solana")):
            print(f"  {line}")
    from .overrides import apply_overrides, gift_rules, load_overrides

    for note in apply_overrides(rows, load_overrides(Path("overrides.yaml")) + gift_rules(gifts)):
        print(f"  {note}")
    match_lines: list[str] = []
    if not args.no_match:
        from .match import match_flows

        matches = match_flows(rows, settings.matching)
        print(f"\nheuristic matches: {len(matches)}")
        for m in matches:
            print(f"  {m.score:.2f}  {m.detail}")
            match_lines.append(f"score {m.score:.2f} — {m.detail}")
    n = write_csv(Path(args.out), rows, labels)
    from .positions import build_positions
    from .report import write_positions_csv, write_summary

    positions = build_positions(rows)
    out = Path(args.out)
    summary_path = Path(args.summary) if args.summary else out.with_name(out.stem + "_summary.md")
    write_positions_csv(out.with_name(out.stem + "_positions.csv"), positions)
    write_summary(summary_path, rows, positions, unknown, names, labels, stats, matches=match_lines,
                  exchange_checks=getattr(args, "exchange_checks", None), exchange_links=[*link_notes, *hnotes])
    print(f"\nwrote {summary_path} and {out.with_name(out.stem + '_positions.csv')} ({len(positions)} positions)")
    print(f"\n== classification ({n} rows -> {args.out})")
    print_summary(rows, unknown, names)
    return 0


def _classify_cardano(ws: list[WalletSpec], store: Store) -> tuple[list[Classified], dict[str, int]]:
    from .decode.cardano_orders import (CardanoState, classify_cardano_wallet, classify_reward_account,
                                        finalize_orders, load_scripts)
    from .registry import Registry

    chain = ws[0].chain
    fetcher = CardanoFetcher(chain, store)
    stakes = sorted(set(fetcher.resolve([w.address for w in ws]).values()))
    txs = store.load_txs(chain.name)
    discovered: dict[str, tuple[bool | None, str]] = {}
    for t in txs.values():
        for so in t.meta.get("script_outputs", {}).values():
            discovered[so["address"]] = (True, "")
        for a in t.meta.get("script_inputs", {}).values():
            discovered[a] = (True, "")
    ctx = RuleContext(chain=chain, wallets=set(stakes), registry=Registry(discovered=discovered))
    state = CardanoState(scripts=load_scripts())
    rows: list[Classified] = []
    for st in stakes:
        rmoves = store.load_movements(chain.name, reward_account(st))
        if rmoves:
            state.reward_accounts.add(st)
            rows.extend(classify_reward_account(st, rmoves, txs))
    for st in stakes:
        rows.extend(classify_cardano_wallet(st, store.load_movements(chain.name, st), txs, ctx, state))
    finalize_orders(state, fetcher.koios.spent_status)
    return rows, dict(state.unknown_scripts)


def cmd_compare(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .coinledger import load_coinledger, load_ours, report

    cl = load_coinledger(Path(args.coinledger))
    ours = load_ours(Path(args.classified))
    stats = report(cl, ours, Path(args.out), Path(args.diff_csv))
    print("CoinLedger comparison (validation only): " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    print(f"wrote {args.out} and {args.diff_csv}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="classify", description="On-chain transaction classifier")
    p.add_argument("--db", help="SQLite path (default from settings.yaml)")
    p.add_argument("--settings", help="alternative settings.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch, cache and normalize history")
    f.add_argument("--addresses", required=True)
    f.add_argument("--chain", help="override the Chain column for every row")
    f.add_argument("--refresh", action="store_true", help="re-fetch the newest (tail) page of each endpoint")
    f.add_argument("--no-names", action="store_true", help="skip verified-contract-name lookups")
    f.add_argument("--top", type=int, default=30)

    v = sub.add_parser("verify", help="compare computed balances with live on-chain balances")
    v.add_argument("--addresses", required=True)
    v.add_argument("--chain")
    v.add_argument("--all", action="store_true", help="include assets whose computed balance is zero")

    mv = sub.add_parser("movements", help="export the normalized movements table")
    mv.add_argument("--out", default="movements.csv")
    mv.add_argument("--chain", dest="chain_name")

    r = sub.add_parser("run", help="fetch (cache-first), classify, write classified.csv")
    r.add_argument("--addresses", required=True)
    r.add_argument("--chain", help="override the Chain column for every row")
    r.add_argument("--out", default="classified.csv")
    r.add_argument("--refresh", action="store_true")
    r.add_argument("--no-names", action="store_true")
    r.add_argument("--top", type=int, default=0)
    r.add_argument("--no-prices", action="store_true", help="skip USD enrichment")
    r.add_argument("--summary", help="summary markdown path (default: <out>_summary.md)")
    r.add_argument("--no-match", action="store_true", help="skip heuristic matching")

    c = sub.add_parser("compare", help="validation-only comparison of classified.csv against a CoinLedger export")
    c.add_argument("--coinledger", required=True, help="CoinLedger transactions CSV export")
    c.add_argument("--classified", default="classified.csv")
    c.add_argument("--out", default="coinledger_comparison.md")
    c.add_argument("--diff-csv", default="coinledger_comparison.csv")

    rc = sub.add_parser("reconcile", help="custody check: holdings vs live balances, negative balances, boundary flows")
    rc.add_argument("--classified", default="classified.csv")
    rc.add_argument("--out", default="reconcile.md")
    rc.add_argument("--holdings", default="holdings.csv")
    rc.add_argument("--min-usd", type=float, default=1.0, help="hide priced boundary flows below this USD value")

    b = sub.add_parser("basis", help="cost basis: lots, gains, income, 2025 per-wallet allocation")
    b.add_argument("--classified", default="classified.csv")
    b.add_argument("--manual", default="manual.yaml", help="user-confirmed events on unfetchable chains")
    b.add_argument("--out-dir", default="basis")
    b.add_argument("--method-pre", default="hifo", choices=["hifo", "fifo"], help="universal method through 2024 (as filed)")
    b.add_argument("--method-post", default="hifo", choices=["hifo", "fifo"], help="per-wallet method from 2025")

    tx = sub.add_parser("tax", help="IRS Form 8949/Schedule D/Schedule 1 PDFs, TaxAct/TurboTax CSVs, reports")
    tx.add_argument("--year", type=int, nargs="+", required=True)
    tx.add_argument("--classified", default="classified.csv")
    tx.add_argument("--manual", default="manual.yaml")
    tx.add_argument("--profile", default="tax_profile.yaml", help="name, filing status, 1099-DA brokers, carryovers")
    tx.add_argument("--out-dir", default="tax")
    tx.add_argument("--method-pre", default="hifo", choices=["hifo", "fifo"])
    tx.add_argument("--method-post", default="hifo", choices=["hifo", "fifo"])

    sv = sub.add_parser("serve", help="local web app: portfolio, tax reports and exports, 1099-DA import")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--method-pre", default="hifo", choices=["hifo", "fifo"])
    sv.add_argument("--method-post", default="hifo", choices=["hifo", "fifo"])

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--") and not {"-h", "--help"} & set(argv[:1]) \
            and not any(a in ("fetch", "verify", "movements", "run", "compare", "reconcile", "basis", "tax", "serve") for a in argv):
        argv.insert(0, "run")  # `classify --addresses a.csv --out classified.csv`
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings(Path(args.settings) if args.settings else None)
        store = Store(Path(args.db) if args.db else settings.db_path)
        handler = {"fetch": cmd_fetch, "verify": cmd_verify, "movements": cmd_movements, "run": cmd_run,
                   "compare": cmd_compare, "reconcile": cmd_reconcile, "basis": cmd_basis, "tax": cmd_tax, "serve": cmd_serve}[args.cmd]
        return handler(args, settings, store)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
