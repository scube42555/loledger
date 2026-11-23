# CLAUDE.md

Guide for working on **loledger** ("Loledger by Scube"). It fetches the user's full on-chain and exchange
history, classifies every token movement, runs a cost-basis engine, and produces IRS tax forms plus a local
web app. User-facing docs (setup, private files, commands, output columns) live in `README.md`. This file
covers how the code fits together and the rules to keep when changing it.

## Commands

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                  # no network: fake transports + hand-built fixtures (tests/fakes.py, tests/fixtures/)
.venv/bin/mypy src tests             # strict; keep it clean
loledger --addresses addresses.csv --out classified.csv    # = `loledger run`: fetch (cache-first) + classify
loledger run --addresses addresses.csv --refresh           # also pick up new activity (tail page of each history)
loledger run --addresses addresses.csv --only OKX          # fetch just this wallet/chain/exchange; the rest from the store
loledger basis | tax --year 2025 | serve | verify | reconcile | compare --coinledger <csv>
```

- Run everything from the project root. `overrides.yaml`, `manual.yaml`, `tax_profile.yaml`, `data/` and
  `exports/` are all resolved relative to the working directory.
- `python -m loledger.cli …` is the same as the `loledger` script. The web app's sync uses it.
- Never commit the user's data. `addresses.csv`, `overrides.yaml`, `manual.yaml`, `tax_profile.yaml`,
  `exchange_deposits.yaml`, `classified*`, `basis/`, `tax/`, `exports/` and `data/` are gitignored and are the
  user's real financial records. Change the `*.example.*` samples instead.

## Pipeline

```
fetch ──► normalize ──► rules ──► decode ──► prices ──► match ──► classified.csv ──► costbasis ──► taxforms / web
(cache)   (movements)   (1.0)    (orders)   (USD)      (<1.0)
```

1. **Fetch** (`adapters/<chain>/fetch.py`): every API response is cached in SQLite (`data/txclass.sqlite`,
   `store.py`), so re-runs make no network calls. `--refresh` re-fetches only the newest page.
   - **EVM:** Routescan/Etherscan account lists plus an RPC receipt per tx. A full page is cut before its last
     block, so no rows are lost or duplicated even though the explorer gives no log index.
   - **Avalanche atomic C↔P/X imports/exports** (`adapters/evm/atomic.py`): found by comparing computed
     balances against `balancehistory` and bisecting to the exact block.
   - **Cardano:** Koios `account_txs` + `tx_info` + `datum_info`. A wallet is a stake key; addr1 rows resolve
     to it. Partial-fill txs that never pay the wallet are found by walking back from the final fill.
   - **Other chains:** Solana (stake accounts expanded per wallet), Sui, Algorand, Injective, Hedera, Chia,
     and UTXO chains (Bitcoin, Doge, Kaspa).
   - **Cosmos SDK chains** (`adapters/cosmos/`, per-chain `PROFILES` in `fetch.py`): Cosmos Hub and dYdX.
     dYdX's archive (Polkachu) allows tx search by address. Its Ethereum-bridge credits have no tx: they are rebuilt
     from the proposer's MsgAcknowledgeBridges (listed by full hash in `exports/dydx/transactions.csv`) at
     ack height + `delay_blocks`, as `bridge:<event id>`. Unbonding completions are booked only once their
     completion time has passed.
     Cosmos Hub: public nodes prune history and the CryptoCrew archive node has address
     search disabled. So the wallet's txs come from `exports/cosmos/transactions.csv` (height + the explorer's
     shortened hash). Each is resolved by hashing the block's txs (RPC `/block`), then fetched with REST GetTx.
     Both SDK result layouts are converted (per-message `logs` ≤ 0.47; flat events with `msg_index` ≥ 0.50) and
     fed to the Cosmos SDK normalizer shared with Injective (`adapters/injective/normalize.py`, parameterized
     by native denom). IBC receipts are relayed by third parties: the counterparty becomes `ibc:<sender>`, and
     the relayer's memo is kept out of the spam rule's `note`.
   - **Exchanges** (`adapters/exchange/`): Crypto.com through its API (keys in `.env`). MEXC, Gemini, OKX,
     Binance.US and Coinbase through export files under `exports/` (paths and parsers in `settings.yaml`).
     These aren't listed in addresses.csv.
     - **OKX** also reads its API (`okx.py`, us.okx.com by default; a key only works on the OKX site it was made
       on). The API keeps about 3 months, so fetched bills accumulate in the store by bill id. They are shaped
       like export rows and merged in `files.parse_okx` (bill ids match the exports; API rows win, carrying the
       on-chain tx hash and withdrawal fee).
     - **Gemini** also reads its API (`gemini.py`, read-only Auditor key). The export stays the base. API
       trades (`/v1/mytrades`, all pairs since a timestamp) and transfers (`/v1/transfers`, including internal
       "Staking Yield" credits) are fetched from a few days before the newest known record and accumulated in the
       store. `files.parse_gemini` adds only those the export lacks: trades by tid (= the export's Trade ID),
       transfers by (ms timestamp, currency, amount). Live USD can differ by cents, because the export rounds
       amounts for display.
     - **Overlapping export files must never double count.** Merge by the exchange's row id where one exists
       (OKX bill id, Binance.US Transaction_Id). Otherwise use `files.merge_rows`: identical rows within one file
       are separate records, and across files a record counts as often as the file with the most copies has it.
       Keep this rule for every new export parser.
2. **Normalize** (`adapters/<chain>/normalize.py`): one signed leg per asset and counterparty, from the
   wallet's point of view (`model.py`: `Movement`, `TxContext`).
   - **EVM:** wrap and unwrap legs come from WETH9 events.
   - **Cardano:** an output belongs to the wallet only if its payment part is a key. Legs are netted per
     counterparty, and fee, deposit, reward and mint legs are added so every tx reconciles exactly.
3. **Rules** (`rules.py`): deterministic, confidence 1.0, applied in order: failed → spam → wrap → yield → LP
   → swap / signed-order fill → escrow order → transfer, bridge, airdrop or claim.
4. **Decode** (`decode/`): order lifecycles.
   - **EVM** (`evm_orders.py`): KyberSwap by order hash, Gelato through the per-order vault.
   - **Cardano** (`cardano_orders.py`, `cardano_datums.py`): linked exactly by which tx spends the order
     UTxO. Datums are decoded for MuesliSwap, Minswap V1/V2, Spectrum and VyFi.
   - **Sui** (`sui_orders.py`): DeepTrade / DeepBook v3.
   - **Exchanges** (`exchanges.py`): exchange deposits and withdrawals are linked to wallet txs by tx hash,
     falling back to amount/time/deposit address.
5. **Prices** (`prices.py`, `enrich.py`): DefiLlama, cached per coin per hour. The fallbacks are
   `price_aliases` in the registry and the price implied by the wallet's own fills.
6. **Overrides** (`overrides.py`): user-confirmed facts from `overrides.yaml`, plus gift rules (an
   addresses.csv label containing "Gift" marks a recipient: never fetched, and sends to it are gifts).
7. **Match** (`match.py`): heuristic pairing of what's still unlinked, always confidence < 1.0, greedy and
   one-to-one. Tuned under `matching:` in `settings.yaml`.
8. **Outputs** (`report.py`, `positions.py`): `classified.csv`, `classified_positions.csv` and
   `classified_summary.md`.

After classification:
- `costbasis.py` holds the lots, disposals, income, transfers between own accounts and the 2025 per-wallet
  allocation. Account key: the exchange name, or `chain|address`. Parked buckets are `escrow:`, `staked:`,
  `deposit:`, `vault:`, `xp-chain:` and `offlist:`.
- `taxforms.py` builds the tax-year view (`build_year`). Exports are registered in the `EXPORTS` list,
  which drives both `loledger tax` and the web download dialog. It fills the official IRS PDFs (downloaded
  once into `data/irs_forms/`). Values are printed as a text layer and the form fields are removed, except
  boxes left empty (`keep` in `_fill_pages`): those stay fillable, so the user can complete the PDF in a viewer.
  The Form 8949 name/SSN header is a single field shared by every page.
- `form1099da.py`: 1099-DA import and reconciliation (stored in `data/1099da/`).
- `pdfforms.py`: generic fillable-PDF filling (`fill`), overlay for forms without fields (SCDOR), and
  `assemble` for mailing packets. `assemble` flattens filled values with qpdf and draws multi-line boxes itself,
  word-wrapped (qpdf would cut them to one line). Boxes left empty stay fillable, named `<part>-<field>`.
  `add_blank_fields` adds fillable boxes to forms that have none (SCDOR), at measured rects. A field named
  `shared_<x>` stays one field across a whole packet (the SSN typed once fills every form). It must not set
  NeedAppearances, or qpdf redraws SC Schedule AMD's barcode field as plain digits.
  - Amended-return packets are built OUTSIDE `src/`, by the user's own gitignored scripts in
    `exports/amended/`: `fill_*.py` (one per form and year, calling `pdfforms.fill`/`overlay`) write the
    parts, then `build_packets.py` assembles them in mailing order. `AMENDMENT_NOTES.md` there explains the
    filings. Look there before changing anything packet-related, and re-run those scripts after changing
    `pdfforms`. They hold the user's personal data, so never copy their contents into tracked files.
- `reconcile.py` checks custody (holdings vs live balances). `coinledger.py` writes the CoinLedger
  comparison report.

## Web app (`loledger serve`, 127.0.0.1 only)

- `web/app.py` holds the routes. `web/data.py` (`AppData`) holds one cost-basis run, live prices (5-minute
  cache) and per-year `TaxYear` caches. Clear `_years[year]` when anything that affects a year changes.
- **Pages:**
  - **Portfolio:** `/portfolio?account=…`.
  - **Accounts:** `/accounts`, backed by `web/accounts.py`. Wallets are grouped by chain (largest balance first) and read straight
    from addresses.csv, with exchanges listed separately. The Add form appends to addresses.csv. Re-sync
    runs the CLI in a background subprocess, one job at a time (`Syncer`). For one wallet that's
    `fetch --refresh` on `data/sync_one.csv` and then `run`; for all wallets it's `run --refresh`. Then
    `AppData.refresh()` reloads, and the page polls `/accounts/sync/status`. One wallet or exchange syncs with
    `run --only <address|exchange>` (a wallet adds --refresh). Only that one is fetched. Everything else is
    re-classified from the store, which gives the same output as a full run in ~20 s instead of minutes.
  - **Tax reports:** `/tax` redirects to the last year viewed (the `tax_year` cookie) or else the most recent
    year. `/tax/<year>` has ←/→ buttons and arrow-key navigation, the export dialog, the Form 8949 summary
    totals, and the year's Form 1099-DA section (add, reconcile, remove). `/1099-da` only redirects there.
- Templates are in `web/templates/` (`base.html` holds the header, nav, theme switcher and the shared
  `lineChart`). The logo is `_logo.html` (inline SVG coloured by the `--logo-a`/`--logo-b` CSS variables),
  with `static/favicon.svg`. Styles are in `static/app.css` (light/dark through CSS variables; keep both
  themes working).
- To check a UI change visually: start `loledger serve --port 8799` in the background, then run
  `chromium --headless --screenshot=… --window-size=1400,2000 http://127.0.0.1:8799/<page>`.

## Tax and accounting rules (user decisions: don't change without asking)

- **Method:** HIFO across all wallets (universal) through 2024, matching the returns as filed. From
  2025-01-01, HIFO per wallet/account after a Rev. Proc. 2024-28 allocation of unused basis that traces each
  unit to the wallet that held it.
- **Network fees** paid in crypto, including on self-transfers, are small taxable disposals at FMV.
- **Not taxable:** bridges, wraps (ETH↔WETH) and transfers between own wallets. DEX limit orders are
  placement → fill, not withdraw/deposit.
- **ETH validator income** is booked at each withdrawal sweep, with no daily accrual. Solana and Cardano
  rewards are booked per epoch credit.
- **Form 8949 boxes:** C/F through 2024. From 2025, H/K for 1099-DA brokers (G/J when the form reports basis)
  and I/L for everything else. Futures P&L goes in C/F.
- **CoinLedger is validation-only.** Its data never feeds classification or basis; `compare` reports
  differences and nothing more.
- **Gas** goes in the `gas_fee_native`/`gas_fee_usd` columns, not in separate fee rows. Always fetch full
  history.
- **Unsupported chains** (e.g. Osmosis) still belong in addresses.csv. They're skipped with a warning and their
  events go in `manual.yaml` until an adapter exists.

## Extending

- **Unknown contract or script in the summary:** add it to `registry/evm_<chain>.yaml` (`kind` is one of
  router, settlement, order_book, order_router, escrow, lp_pool, lp_nft, bridge, yield_vault, staking,
  exchange, spam_distributor, limit_order_protocol). For Cardano, add it to `registry/cardano.yaml` under
  `scripts`.
- **Missing price:** add an entry to `price_aliases` in the chain's registry file.
- **New EVM chain:** add a block to `settings.yaml` (chain id, explorer, RPC, DefiLlama `price_chain`,
  `native_price_id`) and a registry YAML for it.
- **New non-EVM chain:** add `adapters/<chain>/{fetch,normalize}.py` producing `Movement`/`TxContext`. Wire
  it into `cli.py` (`cmd_fetch`, the `cmd_run` chain loop, and `cmd_verify` for live balances) and add tests
  with fixtures. Ask which wallets and DEXes were used on the chain before writing decoders.
- **New exchange export:** add a `parse_<name>` in `adapters/exchange/files.py`, register it in the `parsers`
  dict in `cli._load_file_exchanges`, add a `settings.yaml` block
  (`kind: exchange`, `export`, `parser`, `tz_hours`), and add the name to `EXCHANGES` in `costbasis.py`.
- **New tax export:** write a `(TaxYear) -> bytes` function in `taxforms.py` and append an `Export(...)` to
  `EXPORTS` (tab `irs`, `software` or `csv`). It then shows up in the CLI and the web dialog.

## Gotchas

- The `"txclass"` namespace in `store.put_raw`/`get_raw` and the `data/txclass.sqlite` file name predate the
  rename to loledger. Keep them: renaming orphans the user's cache.
- Money is `Decimal` throughout. Round to cents per Form 8949 line (`_cents`) before summing, so the box
  totals equal the sum of the lines.
- EVM addresses are lower-cased internally; addresses.csv may use any case.
- `verify` mismatches on spoofed/spam tokens are expected: their balances don't follow Transfer events.
