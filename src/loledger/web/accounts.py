"""Accounts page backend: the wallets in addresses.csv grouped by chain, adding a wallet, and re-syncing.

A sync runs the CLI in a child process from the project folder (the same commands you would type), one job at
a time, and the app reloads its data when the job succeeds:
- one wallet: `loledger fetch --refresh` for just that wallet, then `loledger run` (cache-first) to re-classify;
- all wallets: `loledger run --refresh`.
Exchange accounts aren't listed in addresses.csv: they come from export files (exports/) or API keys, and are
re-read on every sync.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import GIFT_LABEL, ConfigError, Settings

ADDRESSES = "addresses.csv"
ONE_WALLET_CSV = Path("data/sync_one.csv")  # gitignored (data/)


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


@dataclass
class SyncJob:
    title: str
    steps: list[list[str]]
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=400))

    @property
    def running(self) -> bool:
        return self.finished is None

    def status(self) -> dict[str, Any]:
        return {"title": self.title, "running": self.running, "returncode": self.returncode,
                "started": self.started, "finished": self.finished,
                "elapsed": int((self.finished or time.time()) - self.started), "log": list(self.log)[-60:]}


class Syncer:
    """One sync at a time, in a background thread; `on_success` reloads the app's data."""

    def __init__(self, root: Path, on_success: Callable[[], None]) -> None:
        self.root = root
        self.on_success = on_success
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
        return self._start(f"Re-sync {name}", [self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv")])

    def sync_one(self, w: Wallet) -> SyncJob:
        one = self.root / ONE_WALLET_CSV
        one.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        wr = csv.writer(buf, lineterminator="\n")
        wr.writerow(["Address", "Chain", "Label"])
        wr.writerow([w.address, w.chain, w.label])
        one.write_text(buf.getvalue())
        name = w.label or f"{w.address[:8]}…"
        return self._start(f"Re-sync {name} ({w.chain})", [
            self._cli("fetch", "--addresses", str(ONE_WALLET_CSV), "--refresh", "--top", "0"),
            self._cli("run", "--addresses", ADDRESSES, "--out", "classified.csv")])

    def _start(self, title: str, steps: list[list[str]]) -> SyncJob:
        with self.lock:
            if self.busy:
                raise RuntimeError("A sync is already running.")
            self.job = job = SyncJob(title, steps)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: SyncJob) -> None:
        rc = 0
        try:
            for cmd in job.steps:
                job.log.append("$ loledger " + " ".join(cmd[3:]))
                proc = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
                assert proc.stdout is not None
                for line in proc.stdout:
                    job.log.append(line.rstrip("\n"))
                rc = proc.wait()
                if rc != 0:
                    job.log.append(f"(stopped: exit code {rc})")
                    break
            if rc == 0:
                job.log.append("Reloading portfolio and tax data…")
                self.on_success()
                job.log.append("Done.")
        except Exception as e:  # keep the server up; show the failure on the page
            rc = rc or 1
            job.log.append(f"error: {e}")
        job.returncode = rc
        job.finished = time.time()
