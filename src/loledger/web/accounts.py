"""Accounts page backend: the wallets in addresses.csv grouped by chain, adding a wallet, and re-syncing.

A sync runs the CLI in a child process from the project folder (the same commands you would type), one job at
a time, and the app reloads its data when the job succeeds:
- one wallet or exchange: `loledger run --only <address or exchange>` (a wallet also with --refresh): only it is
  fetched, everything else is re-classified from the local store;
- all wallets: `loledger run --refresh`.
After a one-account sync, transfers it brought in whose other side is another known account (a wallet in
addresses.csv or an exchange) and isn't in classified.csv yet get that account synced too, in a follow-up
`run --refresh --only <those accounts>`. The follow-up's own new transfers are checked the same way, round after
round, until a round finds nothing; each account is synced at most once, so at worst it reaches a Sync All.
Exchange accounts aren't listed in addresses.csv: they come from export files (exports/) or API keys, and are
re-read on every sync.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import GIFT_LABEL, ConfigError, Settings, display_address
from ..decode.exchanges import norm_symbol as xnorm

ADDRESSES = "addresses.csv"
LAST_LOG = "data/last_sync.log"  # the latest sync's full log


@dataclass
class Wallet:
    address: str
    chain: str  # canonical chain name when supported, else what addresses.csv says
    label: str
    supported: bool
    gift: bool
    kind: str = ""

    @property
    def key(self) -> str:
        a = self.address.lower() if self.kind == "evm" else self.address
        return f"{self.chain}|{a}"


def read_wallets(root: Path, settings: Settings) -> list[Wallet]:
    p = root / ADDRESSES
    if not p.exists():
        return []
    out: list[Wallet] = []
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            addr, chain, label = r.get("address", ""), r.get("chain", ""), r.get("label", "")
            if not addr:
                continue
            try:
                c = settings.resolve_chain(chain)
                out.append(Wallet(addr, c.name, label, True, bool(GIFT_LABEL.search(label)), c.kind))
            except ConfigError:
                out.append(Wallet(addr, chain or "(no chain)", label, False, bool(GIFT_LABEL.search(label))))
    return out


def chain_choices(settings: Settings) -> list[str]:
    """Chains a wallet can be added for (exchanges come from exports/API keys, not addresses.csv)."""
    return sorted(n for n, c in settings.chains.items() if c.kind != "exchange")


def validate(settings: Settings, chain: str, address: str, existing: list[Wallet]) -> tuple[str, str]:
    """(chain name to write, normalized address); raises ValueError with a readable message."""
    address, chain = address.strip(), chain.strip()
    if not address or not chain:
        raise ValueError("Enter both the address and its chain.")
    if any(ch in address for ch in " \t\r\n,\"'"):
        raise ValueError("The address can't contain spaces, commas or quotes.")
    try:
        c = settings.resolve_chain(chain)
    except ConfigError:
        c = None  # unsupported chain (e.g. Kaspa before its adapter existed): kept in the list, not synced
    if c is not None:
        chain = c.name
        if c.kind == "evm" and not (address.startswith("0x") and len(address) == 42):
            raise ValueError(f"{chain} addresses start with 0x and are 42 characters long.")
        if c.kind == "cardano" and not address.startswith(("addr1", "stake1")):
            raise ValueError("Cardano wallets are mainnet addr1… or stake1… addresses.")
        if c.kind == "exchange":
            raise ValueError(f"{chain} is an exchange: add its export under exports/ or its API key in .env instead.")
    norm = address.lower() if c is not None and c.kind == "evm" else address
    for w in existing:
        if w.chain.lower() == chain.lower() and (w.address.lower() if w.kind == "evm" else w.address) == norm:
            raise ValueError(f"{address} is already listed for {chain}" + (f" as “{w.label}”." if w.label else "."))
    return chain, address


def append_wallet(root: Path, chain: str, address: str, label: str) -> None:
    p = root / ADDRESSES
    if not p.exists():
        p.write_text("Address,Chain,Label\n")
    text = p.read_text()
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([address, chain, label.strip()])
    with p.open("a", newline="") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(buf.getvalue())


# -- counterpart accounts of new transfers ------------------------------------------------------
LegKey = tuple[str, str, str, str]

# exchange network names -> chain (settings name); anything else is tried as a chain name / alias
NETWORKS = {"ERC20": "Ethereum", "ETH": "Ethereum", "ETHEREUM": "Ethereum", "CCHAIN": "Avalanche", "AVAXC": "Avalanche",
            "AVAX": "Avalanche", "AVALANCHE": "Avalanche", "SOL": "Solana", "SPL": "Solana", "SOLANA": "Solana",
            "MATIC": "Polygon", "POL": "Polygon", "POLYGON": "Polygon", "ARB": "Arbitrum", "ARBITRUM": "Arbitrum",
            "ADA": "Cardano", "CARDANO": "Cardano", "BTC": "Bitcoin", "BITCOIN": "Bitcoin", "DOGE": "Doge",
            "DOGECOIN": "Doge", "KAS": "Kaspa", "KASPA": "Kaspa", "KRC20": "Kaspa", "XCH": "Chia", "CHIA": "Chia",
            "SUI": "Sui", "ALGO": "Algorand", "ALGORAND": "Algorand", "HBAR": "Hedera", "HEDERA": "Hedera",
            "INJ": "Injective", "INJECTIVE": "Injective", "ATOM": "Cosmos", "COSMOS": "Cosmos"}


def network_chain(net: str, settings: Settings) -> str:
    """The chain an exchange's network label means ("USDT-ERC20", "BTC-Bitcoin", "CCHAIN", "ethereum"), or "" when
    there is none; an unrecognised label comes back as given (a chain we don't fetch, e.g. BEP20)."""
    n = net.strip()
    if not n:
        return ""
    tail = n.rsplit("-", 1)[-1] if "-" in n else n
    head, _, paren = n.partition("(")  # MEXC website: "Bitcoin(BTC)", "Ethereum(ERC20)"
    for cand in (n, tail, head.strip(), paren.rstrip(")").strip()):
        if not cand:
            continue
        if cand.upper() in NETWORKS:
            return NETWORKS[cand.upper()]
        try:
            return settings.resolve_chain(cand).name
        except ConfigError:
            pass
    return n


@dataclass(frozen=True)
class Gap:
    """One transfer whose other side is missing; sync = whether syncing that account can fill it."""
    text: str
    sync: bool = True


def leg_keys(path: Path) -> set[LegKey]:
    """(chain, tx, leg, wallet) of every row in classified.csv: the before-picture a sync is compared with."""
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {(r["chain"], r["tx_hash"], r["leg_id"], r["wallet"]) for r in csv.DictReader(f)}


def _key(addr: str) -> str:
    return addr.lower() if addr.startswith("0x") else addr


def api_exchanges(settings: Settings) -> list[str]:
    """Exchanges read live through their API (keys in .env): a sync fetches their latest days."""
    from ..adapters.exchange import gemini, okx

    keys = {"okx": okx.has_keys(), "gemini": gemini.has_keys(),
            "Crypto.com": bool(os.environ.get("CRYPTOCOM_API_KEY") and os.environ.get("CRYPTOCOM_API_SECRET"))}
    return [c.name for c in settings.chains.values()
            if c.kind == "exchange" and (keys.get(c.parser) or keys.get(c.name))]


def counterparts(path: Path, settings: Settings, wallets: list[Wallet], before: set[LegKey], synced: Iterable[str],
                 closed: Iterable[str] = (), api: Iterable[str] = ()) -> dict[str, str]:
    """Accounts on the other side of transfers that are new since `before` and whose side is still missing:
    {name for `run --only`: the first such transfer}. See `missing_sides`."""
    gaps = missing_sides(path, settings, wallets, before, synced, closed, api)
    return {k: next(g.text for g in v if g.sync) for k, v in gaps.items() if any(g.sync for g in v)}


def unpaired(path: Path, settings: Settings, wallets: list[Wallet], closed: Iterable[str] = ()) -> dict[str, list[Gap]]:
    """The whole history: every transfer whose other side is a known account that doesn't have it, by account.
    (Unknown senders are left out here: over the full history they would flag every outside deposit.)"""
    return missing_sides(path, settings, wallets, set(), (), closed)


def missing_sides(path: Path, settings: Settings, wallets: list[Wallet], before: set[LegKey], synced: Iterable[str],
                  closed: Iterable[str] = (), api: Iterable[str] = ()) -> dict[str, list[Gap]]:
    """Transfers not in `before` whose other side is a known account still missing them.

    Returns {name for `run --only`: [one line per transfer]}: a wallet address (every addresses.csv row with it) or an exchange name.
    Found from the exchange named in to_/from_exchange subtypes, a wallet leg's counterparty address, the
    destination address of an exchange withdrawal, or a receipt from an address that paid out an earlier
    (matched) withdrawal from that exchange. Also: a wallet whose row only stands in for a tx it signed
    (`awaiting_sync`), and, for a deposit from an address nobody recognises, the `api` exchanges (it may be one of
    their withdrawals; their sync only fetches the latest days). Skipped: the accounts just synced, gift recipients, unsupported
    chains, closed exchanges, and transfers already linked or whose tx the other wallet already has."""
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        rows = [{k: v or "" for k, v in r.items()} for r in csv.DictReader(f)]
    exchanges: dict[str, str] = {}
    for c in settings.chains.values():
        if c.kind == "exchange":
            for n in (c.name, *c.aliases):
                exchanges[n.lower()] = exchanges[n.lower().replace(".", "").replace(" ", "")] = c.name
    skip = {n.strip().lower() for n in synced} | {n.lower() for n in closed}
    ok = [w for w in wallets if w.supported and not w.gift]
    by_addr: dict[str, list[Wallet]] = defaultdict(list)
    for w in ok:
        by_addr[_key(w.address)].append(w)
    by_label: dict[tuple[str, str], list[Wallet]] = defaultdict(list)
    for w in ok:
        if w.label:
            by_label[(w.chain, w.label)].append(w)
    for r in rows:  # wallet ids that differ from the listed address (a Cardano addr1 row is fetched as its stake key)
        wid = _key(r["wallet"].split("#")[0])
        lab = r["wallet_label"].replace(" · rewards", "")
        if wid not in by_addr and (r["chain"], lab) in by_label:
            by_addr[wid] = by_label[(r["chain"], lab)]
    # a wallet "has" a tx only with a real leg in it: a fee-only row can be a stand-in built from another
    # wallet's fetch (Cardano: a tx this wallet signed, seen before its own history was re-fetched)
    ids: dict[str, set[str]] = defaultdict(set)  # listed address -> every id its rows use (Cardano: + stake key)
    for k, ws in by_addr.items():
        for w in ws:
            ids[w.address].add(k)
    has_tx = {(r["chain"], _key(r["wallet"].split("#")[0]), r["tx_hash"]) for r in rows if r["amount"].strip("-0.")}
    # chains that give each side its own tx id (Chia): the other wallet's mirror leg, same time, pointing back here
    mirror = {(r["chain"], _key(r["wallet"].split("#")[0]), _key(r["counterparty"]), r["timestamp"],
               abs(Decimal(r["amount"]))) for r in rows if r["amount"].strip("-0.")}
    # exchange payout (hot) wallets, learned from withdrawals already matched to a wallet: an unmatched receipt
    # from one of them is likely a withdrawal whose exchange side isn't synced yet (used to pick syncs only)
    payout = {(r["chain"], _key(r["counterparty"])): ex for r in rows
              if r["subtype"].startswith("from_exchange:") and r["linked_tx_hash"] and r["chain"] not in exchanges.values()
              and (ex := exchanges.get(r["subtype"].split(":", 1)[1].lower()))}
    out: dict[str, list[Gap]] = {}

    def add(name: str, r: dict[str, str], where: str, text: str = "", sync: bool = True) -> None:
        if name.lower() in skip:
            return
        if not text:
            amt = f"{abs(Decimal(r['amount'])).normalize():f}"
            text = (f"{amt} {r['token']} {'to' if r['amount'].startswith('-') else 'from'} {where} "
                    f"({r['wallet_label'] or r['chain']} {r['tx_hash'][:18]}…)")
        gap = Gap(f"{r['date_utc'][:10]}: {text}", sync)
        if gap not in out.setdefault(name, []):
            out[name].append(gap)

    for r in rows:
        if (r["chain"], r["tx_hash"], r["leg_id"], r["wallet"]) in before:
            continue
        if r["subtype"] == "awaiting_sync":  # what this wallet sent is only known once its own history is fetched
            for w in by_addr.get(_key(r["wallet"].split("#")[0]), []):
                add(w.address, r, "", f"{w.label or w.address} signed {r['tx_hash'][:18]}…, seen only through "
                                      "another wallet's sync")
            continue
        if r["linked_tx_hash"] \
                or r["subtype"].startswith("spam") or r["leg_role"] == "fee" or not r["amount"].strip("-0."):
            continue
        sub = r["subtype"]
        if sub.startswith(("to_exchange:", "from_exchange:")):
            ex = exchanges.get(sub.split(":", 1)[1].lower())
            if ex:
                add(ex, r, ex)
            continue
        if r["chain"] in exchanges.values():  # the exchange names the address, not the chain: one line for all its rows
            dest = by_addr.get(_key(r.get("withdrawal_address", "")), [])
            if dest:
                amt = f"{abs(Decimal(r['amount'])).normalize():f}"
                what = f"{r['chain']} withdrawal of {amt} {r['token']}"
                rec = f"its record {r['tx_hash'][:30]}…"
                net = network_chain(r.get("withdrawal_network", ""), settings)
                on = [w for w in dest if w.chain == net] if net else \
                    [w for w in dest if xnorm(settings.chains[w.chain].native_symbol) == xnorm(r["token"])]
                if net and not on:  # sent on a chain this address isn't listed for: nothing to sync
                    add(dest[0].address, r, "", f"{what} to {display_address(dest[0].address)} on {net}, a chain this "
                        f"address isn't listed for in addresses.csv ({rec})", sync=False)
                elif len(on) == 1:  # the network the exchange names, or the only chain where the token is native
                    w = on[0]
                    add(w.address, r, "", f"{what} to {w.label or w.address} ({w.chain}); that wallet has no matching "
                                          f"receipt ({rec})")
                else:
                    to = " / ".join(dict.fromkeys(f"{w.label} ({w.chain})" if w.label else w.chain for w in dest))
                    add(dest[0].address, r, "", f"{what} to one of {to}: the export doesn't say which network; none "
                                                f"of them has a matching receipt ({rec})")
            continue
        cp = _key(r["counterparty"])
        for w in by_addr.get(cp, []):
            me = _key(r["wallet"].split("#")[0])
            if w.chain == r["chain"] and not any((w.chain, i, r["tx_hash"]) in has_tx for i in ids[w.address]) and \
                    (w.chain, cp, me, r["timestamp"], abs(Decimal(r["amount"]))) not in mirror:
                add(w.address, r, w.label or w.address)
        incoming = not r["amount"].startswith("-")
        ex = payout.get((r["chain"], _key(r["counterparty"])))
        if ex and incoming and r["leg_role"] != "min_ada":  # min-ADA rides along with a token leg
            add(ex, r, f"{ex} (its payout address {display_address(r['counterparty'])[:12]}…)")
        elif incoming and r["classification"] == "TRANSFER" and sub in ("", "from_eoa") \
                and r["leg_role"] != "min_ada" and _key(r["counterparty"]) not in by_addr:
            for ex in api:  # an unknown sender: maybe an exchange withdrawal whose record isn't fetched yet
                add(ex, r, f"{display_address(r['counterparty'])[:12]}… (unrecognised: maybe a {ex} withdrawal)")
    return out


@dataclass
class SyncJob:
    title: str
    steps: list[list[str]]
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    log: list[str] = field(default_factory=list)  # every line, kept for the whole job

    @property
    def running(self) -> bool:
        return self.finished is None

    def status(self, since: int = 0) -> dict[str, Any]:
        """`log` holds the lines from `since` on (the page appends them); `lines` is the total so far."""
        n = len(self.log)
        return {"title": self.title, "running": self.running, "returncode": self.returncode,
                "started": self.started, "finished": self.finished,
                "elapsed": int((self.finished or time.time()) - self.started), "log": self.log[min(since, n):n],
                "lines": n}


class Syncer:
    """One sync at a time, in a background thread; `on_success` reloads the app's data."""

    def __init__(self, root: Path, on_success: Callable[[], None], settings: Settings | None = None,
                 closed: Callable[[], Iterable[str]] = lambda: ()) -> None:
        self.root = root
        self.on_success = on_success
        self.settings = settings  # set: one-account syncs also sync the counterpart accounts of new transfers
        self.closed = closed
        self.job: SyncJob | None = None
        self.lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.running

    def _cli(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "loledger.cli", *args]

    def sync_all(self) -> SyncJob:
        return self._start("Re-sync all accounts", [self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv",
                                                              "--refresh")])

    def sync_exchange(self, name: str) -> SyncJob:
        """Exchanges are re-read on every run: API accounts fetch their recent days live, export-file exchanges
        re-read the files under exports/ (download newer exports there first)."""
        return self._start(f"Re-sync {name}", [self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv",
                                                         "--only", name)], follow=[name])

    def sync_accounts(self, names: list[str], title: str) -> SyncJob:
        """These wallets (addresses) and exchanges, then whatever their new transfers lead to."""
        return self._start(title, [self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv", "--refresh",
                                             "--only", *names)], follow=list(names))

    def sync_one(self, w: Wallet) -> SyncJob:
        """New activity for this wallet only (--refresh); everything else is re-classified from the local store."""
        name = w.label or f"{w.address[:8]}…"
        return self._start(f"Re-sync {name} ({w.chain})", [
            self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv", "--refresh", "--only", w.address)],
            follow=[w.address])

    def _start(self, title: str, steps: list[list[str]], follow: list[str] | None = None) -> SyncJob:
        with self.lock:
            if self.busy:
                raise RuntimeError("A sync is already running.")
            self.job = job = SyncJob(title, steps)
        threading.Thread(target=self._run, args=(job, follow), daemon=True).start()
        return job

    def _step(self, job: SyncJob, cmd: list[str]) -> int:
        job.log.append("$ loledger " + " ".join(cmd[3:]))
        proc = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            job.log.append(line.rstrip("\n"))
        rc = proc.wait()
        if rc != 0:
            job.log.append(f"(stopped: exit code {rc})")
        return rc

    def _run(self, job: SyncJob, follow: list[str] | None = None) -> None:
        rc = 0
        out = self.root / "classified.csv"
        try:
            before = leg_keys(out) if follow and self.settings else set()
            for cmd in job.steps:
                rc = self._step(job, cmd)
                if rc != 0:
                    break
            done = list(follow or [])  # each account at most once per sync, so the chain ends
            hop = 0
            while rc == 0 and follow and self.settings:
                more = counterparts(out, self.settings, read_wallets(self.root, self.settings), before, done,
                                    self.closed(), api_exchanges(self.settings))
                if not more:
                    job.log.append("No new transfers with other known accounts." if not hop else
                                   f"Followed transfers through {hop} round(s); nothing further to sync.")
                    break
                hop += 1
                job.log.append(f"Round {hop}: new transfers with {len(more)} other known account(s); syncing them too:")
                job.log.extend(f"  {name}: {why}" for name, why in more.items())
                before = leg_keys(out)  # what this round adds is checked next
                done += more
                rc = self._step(job, self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv",
                                               "--refresh", "--only", *more))
                if rc != 0:  # classified.csv from the rounds that finished stands: still load it
                    job.log.append("Reloading with the rounds that finished…")
                    self.on_success()
            if rc == 0:
                job.log.append("Reloading portfolio and tax data…")
                self.on_success()
                job.log.append("Done.")
        except Exception as e:  # keep the server up; show the failure on the page
            rc = rc or 1
            job.log.append(f"error: {e}")
        job.returncode = rc
        job.finished = time.time()
        try:  # the whole log, kept after the server stops (data/ is git-ignored)
            (self.root / LAST_LOG).parent.mkdir(parents=True, exist_ok=True)
            (self.root / LAST_LOG).write_text(f"{job.title}\n" + "\n".join(job.log) + "\n")
        except OSError:
            pass
