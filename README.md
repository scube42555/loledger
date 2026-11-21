# loledger — on-chain transaction classifier

`loledger` takes a list of wallets, fetches their full on-chain history and labels every token
movement. The labels are transfers, swaps, limit-order placement/fill/cancel, LP deposit and
withdraw, staking, spam and so on. Each label comes with a confidence score and a reason that names
the rule or evidence behind it. The tool links orders to their fills and LP deposits to their
withdrawals, prices everything in USD at the time it happened, and writes a summary report.

Supported chains:
- **EVM:** Avalanche C-Chain (tested), Ethereum, Base and Arbitrum (configured, untested).
- **Cardano**

Rows for other chains (Kaspa, for example) are skipped with a warning until an adapter exists.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env        # only needed for chains whose explorer requires a key
```

| Variable | Needed for |
|---|---|
| `ETHERSCAN_API_KEY` | Ethereum / Arbitrum (Etherscan V2). The free tier does **not** cover Avalanche or Base. |
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

## How it works

```
fetch ──► normalize ──► rules ──► decode ──► prices ──► match ──► positions/report
(cache)   (movements)   (1.0)    (orders)   (USD)      (<1.0)
```

1. **Fetch** (`adapters/*/fetch.py`)
   - **EVM:** Routescan/Etherscan account lists (normal, internal, ERC-20, ERC-721, ERC-1155) and an
     RPC receipt for every tx. A full page is cut before its last block, so no rows are lost or
     duplicated even though the explorer returns no log index.
   - **Avalanche atomic imports/exports:** these are not EVM txs. They are found by comparing
     computed balances with `balancehistory` and bisecting down to the exact block where they
     differ.
   - **Cardano:** Koios `account_txs` plus `tx_info` and `datum_info`. Intermediate partial-fill txs
     that never pay the wallet are found by walking back from the final fill.
2. **Normalize:** one signed leg per asset and counterparty, from the wallet's point of view.
   - **EVM:** wrap and unwrap legs come from WETH9 `Deposit`/`Withdrawal` events.
   - **Cardano:** an output is the wallet's only if its payment part is a key. A script that
     carries the wallet's stake key does not count. Legs are netted per counterparty, and fee,
     deposit, reward and mint legs are added so that every tx reconciles exactly.
3. **Rules** (`rules.py`): fixed rules in this order — failed → spam → wrap → yield → LP → swap or
   signed-order fill → escrow order → transfer, bridge, airdrop or claim.
4. **Decode** (`decode/`)
   - **KyberSwap:** fills are grouped by order hash from `OrderFilled`; `OrderCanceled` gives the
     cancels.
   - **Gelato:** order terms come from `DepositToken`, and placements are linked to their
     cancel/fill through the per-order vault.
   - **Cardano:** the order lifecycle is linked exactly by which tx spends the order UTxO. Datums
     are decoded per venue: MuesliSwap order book, Minswap V1/V2, Spectrum, VyFi.
5. **Prices** (`prices.py`, `enrich.py`): DefiLlama historical prices, cached per coin per hour.
   Fallbacks are bridged-token aliases and the price implied by the wallet's own fills within 3 days.
6. **Match** (`match.py`): pairs left unlinked are scored on value ratio, time gap, shared
   counterparty script and decoded minimum receive. Assignment is greedy and one-to-one. Matches
   always get confidence below 1.0, and a pair with weak evidence stays unmatched.
7. **Positions** (`positions.py`) and **report** (`report.py`).

`verify` reconciles computed balances against live chain state. On the sample wallets every
Cardano stake key reconciles exactly. Avalanche reconciles except for spoofed/spam tokens, whose
on-chain balances don't follow their Transfer events.

## Extending

- **Unknown contract or script in the summary:** add it to `src/loledger/registry/evm_<chain>.yaml`
  (`kind` is one of router, settlement, order_book, order_router, escrow, lp_pool, lp_nft, bridge,
  yield_vault, staking, exchange, spam_distributor, limit_order_protocol). For Cardano, add it to
  `registry/cardano.yaml` under `scripts`.
- **Missing price:** add an entry to `price_aliases` in the chain's registry file.
- **New EVM chain:** add a block to `settings.yaml` with chain id, explorer, RPC URL, DefiLlama
  `price_chain` and `native_price_id`. Then add a registry YAML for it.
- **New non-EVM chain (e.g. Kaspa):** write an adapter under `src/loledger/adapters/<chain>/` that
  produces `Movement` and `TxContext` objects (see `model.py`). The rules, prices, matching and
  report layers are shared across chains.
- **Tuning heuristics:** edit `matching:` in `settings.yaml`.

## Development

```bash
.venv/bin/pytest -q          # 68 tests, no network (fake transports + hand-built fixtures)
.venv/bin/mypy src tests     # strict
```
