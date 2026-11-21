# loledger — on-chain transaction classifier

`loledger` takes a list of wallets, fetches their full on-chain history and labels every token
movement. The labels are transfers, swaps, limit-order placement/fill/cancel, LP deposit and
withdraw, staking, spam and so on. Each label comes with a confidence score and a reason that names
the rule or evidence behind it. The tool links orders to their fills and LP deposits to their
withdrawals, prices everything in USD at the time it happened, and writes a summary report.

Supported:
- **EVM:** Avalanche C-Chain, Ethereum, Base, Polygon, Arbitrum.
- **Other chains:** Cardano, Solana, Sui, Algorand, Injective, Cosmos Hub, dYdX, Hedera, Chia, Bitcoin, Doge, Kaspa.
- **Exchanges:** Crypto.com and OKX through their APIs (read-only keys in `.env`), and MEXC, Gemini, OKX,
  Binance.US and Coinbase through export files in `exports/`. You can drop newer exports next to older ones:
  overlapping date ranges are merged and each transaction is counted once.

Wallets on other chains (Osmosis, for example) can still be listed. They are skipped with a warning, and
their events can be entered in `manual.yaml`.

**Cosmos Hub** needs a list of each wallet's transactions in `exports/cosmos/transactions.csv` (columns
`Address,Height,Hash`, plus an optional `Explorer` note). Public Cosmos nodes only keep about the last year,
and the one public archive node can't search by address. So copy each transaction's block height and hash
from a block explorer; the shortened form the explorer shows (`75EFBE5E...A176736671`) is enough. Every
listed transaction is then fetched in full from the archive node. `loledger verify` compares the result with
the live balance, which shows whether any transaction is missing from the list.

**dYdX** is found by address on a public archive node, so no list is needed. Ethereum DYDX migrated through
the bridge is credited without a transaction of its own: list the bridge acknowledgement's full hash in
`exports/dydx/transactions.csv` and the credit is rebuilt from it. Unstaked DYDX returns to the wallet when
its 21-day unbonding ends; it shows up from the first sync after that date.

On top of the classification, a cost-basis engine produces IRS Form 8949, Schedule D and Schedule 1,
import files for tax software, and a local web app (portfolio, accounts, tax reports).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env        # only needed for chains whose explorer requires a key
```

| Variable | Needed for |
|---|---|
| `ETHERSCAN_API_KEY` | Ethereum / Arbitrum (Etherscan V2). The free tier does **not** cover Avalanche or Base. |
| `CRYPTOCOM_API_KEY`, `CRYPTOCOM_API_SECRET` | Crypto.com Exchange account (read-only key). |
| `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` | OKX account (read-only key). US accounts use us.okx.com, the default; set `OKX_API_BASE` for another OKX site. |
| `ROUTESCAN_API_KEY` | Optional; raises Routescan limits (Avalanche works without it). |
| `KOIOS_API_KEY` | Optional; raises Koios limits (the public tier works without it). |

`.env`, `data/`, all `classified*` outputs and wallet export files (`eternl-*`) are gitignored.

## Private files

Your own data stays out of git (see `.gitignore`). Start from the samples:

| File | Sample | Holds |
|---|---|---|
| `addresses.csv` | `addresses.example.csv` | your wallets / accounts (rows labelled "Gift" are recipients, not yours) |
| `overrides.yaml` | `overrides.example.yaml` | user-confirmed facts applied after classification |
| `manual.yaml` | `manual.example.yaml` | events on chains whose history can't be fetched |
| `tax_profile.yaml` | `tax_profile.example.yaml` | name, filing status, 1099-DA brokers, loss carryovers |
| `exchange_deposits.yaml` | `exchange_deposits.example.yaml` | your own deposit addresses at exchanges |
| `.env` | `.env.example` | API keys |

Exchange exports (`exports/`), the database (`data/`), imported 1099-DAs and all generated reports are git-ignored too.

## Input: `addresses.csv`

```csv
Address,Chain,Label
0x1111111111111111111111111111111111111111,Avalanche,main
addr1q...,Cardano,Eternl
stake1u...,Cardano,
```

- `Address` is required. EVM addresses can be any case.
- Cardano rows can be `addr1…` payment addresses or `stake1…` stake addresses. A payment address
  is resolved to its stake key, because a Cardano wallet is a stake key.
- `Chain` is matched case-insensitively against the chain name, its aliases, or its chain id in
  `src/loledger/settings.yaml`.
- `Label` is optional and is copied into the outputs.

## Usage

```bash
loledger --addresses addresses.csv --out classified.csv   # full pipeline (= `loledger run …`)
loledger fetch  --addresses addresses.csv                # fetch + cache + normalize only
loledger verify --addresses addresses.csv                # computed vs live on-chain balances
loledger movements --out movements.csv                   # dump the normalized legs
loledger compare --coinledger exports/coinledger.io/<export>.csv   # validation-only report
```

`compare` checks `classified.csv` against a CoinLedger export and writes `coinledger_comparison.md`
and `coinledger_comparison.csv`. It never feeds CoinLedger data into the classifier.
- **On-chain events** are matched by tx id, per wallet, falling back to asset + amount + time.
- **Exchanges** are compared by daily net flow per asset, because CoinLedger splits orders into fills.
- **Differences** are reported for classification, amounts and income per year. Differences that only
  exist because CoinLedger has no matching category (orders, spam, LP) are listed separately.

Useful flags:
- `--refresh` re-fetches only the newest page of each history, to pick up new activity.
- `--no-prices` skips USD enrichment.
- `--no-match` skips heuristic matching.
- `--chain X` overrides the Chain column for every row.
- `--summary path.md` changes where the summary is written.

Every API response is cached in `data/txclass.sqlite`. Re-running after a rule change makes no
network calls; the run prints its cache hits and misses. Delete that file to start over.

## Outputs

**`classified.csv`** has one row per movement leg, plus one gas-only row for each wallet-sent tx
that moved nothing (approvals, cancels, failed txs, delegation).

| Column | Meaning |
|---|---|
| `chain`, `timestamp`, `date_utc`, `block`, `tx_hash`, `leg_id` | where and when (`leg_id` = log index / UTxO leg) |
| `wallet`, `wallet_label` | EVM address or Cardano stake key |
| `direction`, `token`, `token_address`, `amount` | signed amount (+ in, − out), decimals applied |
| `counterparty` | other side of the leg (address, script address, or `cardano:rewards` / `avalanche:atomic` …) |
| `usd_value_at_time`, `usd_price`, `price_source` | `defillama`, `defillama:<alias>` (fallback id), `implied:own_fill(Nd)` |
| `classification`, `subtype`, `leg_role` | see below; `leg_role` ∈ in, out, fee, min_ada, order, passthrough |
| `linked_tx_hash` | the other half of an order / LP / heuristic pair |
| `order_id`, `order_details` | `kyber:<hash>`, `gelato:<key>`, `cardano:<tx#idx>`; decoded terms, fill price vs limit |
| `protocol`, `confidence`, `reason` | 1.0 = deterministic; < 1.0 = spam/heuristic; reason says why |
| `gas_fee_native`, `gas_fee_usd` | on the first row of each tx the wallet paid for |
| `tx_from`, `tx_to`, `utxo_ref`, `datum_summary` | raw context (Cardano: UTxO refs and a generic datum summary) |

Classifications:
- **`TRANSFER` / `INTERNAL_TRANSFER`**: to or from an outside party / between two listed wallets.
- **`SWAP`**: atomic swap in one tx. The subtype is `market`, or `limit_order_fill` when a third
  party executed it.
- **`ORDER_PLACED` / `ORDER_FILLED` / `ORDER_CANCELLED`**: the order lifecycle. Placement subtypes
  look like `<kind>:<outcome>`, e.g. `limit:partial→filled` or `market:cancelled`.
- **`LP_DEPOSIT` / `LP_WITHDRAW` / `LP_FEES_CLAIMED`**
- **`FAILED`**: a reverted tx. Only gas was paid.
- **`OTHER`**, with a subtype:
  - `wrap` / `unwrap`
  - `bridge_in` / `bridge_out`
  - `atomic_import` / `atomic_export` (Avalanche C↔P/X)
  - `yield_deposit` / `yield_withdraw`
  - `staking_reward_withdrawal`, `stake_registration`, `delegation`
  - `airdrop`
  - `spam_address_poisoning` / `spam_fake_token` / `spam_link_token` / `spam_nft`
  - `approval`, `contract_call:<fn>`

**`classified_positions.csv`** lists LP, yield-vault and Avalanche P/X-Chain positions. For each one
it gives the amounts and USD value deposited and withdrawn, the cost basis (fees included) and the
realized P&L once the position is closed.

**`classified_summary.md`** contains:
- per-wallet totals and gas
- class counts
- order outcomes per venue
- unresolved orders
- heuristic matches
- the positions table
- rows to review (confidence below 1.0)
- unregistered contracts and scripts
- price coverage

## Cost basis, tax forms and the web app

```bash
loledger basis                       # lots, gains, income -> basis/
loledger tax --year 2025             # every export for the year -> tax/ (with a README of what to file)
loledger serve                       # http://127.0.0.1:8765/ (local only)
```

The web app has three pages:
- **Portfolio:** holdings at live prices, cost basis and unrealized gain, per asset and per account.
- **Accounts:** wallets grouped by chain, with value per wallet. You can add a wallet (it is appended to
  `addresses.csv`), re-sync one wallet, or re-sync everything. A sync runs the same `loledger` commands as
  above and shows their output live.
- **Tax reports:** opens the last year you viewed, or the most recent one. ←/→ (buttons or arrow keys)
  switch years. Each year shows its summary, the Form 8949 summary totals, the downloads (IRS PDFs,
  TaxAct/TurboTax files, summary totals, CSV reports), and its Form 1099-DA section, where you add the
  forms your exchanges send and reconcile them against your records.

**E-filing with totals only.** Some software takes summary totals per Form 8949 box instead of importing
every sale (Cash App Taxes, IRS Free File Fillable Forms). For those, enter the rows from
`form_8949_summary_totals_<year>.csv` as one line per box, then mail the line-by-line statement (the pages
after the first in the consolidated Form 8949) with Form 8453.

Not tax advice: review with a tax professional before filing.

## Development

See [`CLAUDE.md`](CLAUDE.md) for the architecture, the project's accounting rules, how to extend it (new
chains, exchanges, contracts, exports), and the test commands.
