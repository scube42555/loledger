"""Settings loading: chain configuration from settings.yaml, secrets from the environment / .env."""

from __future__ import annotations

import csv
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROUTESCAN_URL = "https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api"
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
KOIOS_URL = "https://api.koios.rest/api/v1"

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ChainConfig:
    name: str
    kind: str  # "evm" | "cardano"
    aliases: tuple[str, ...]
    chain_id: int | None
    native_symbol: str
    native_decimals: int
    wrapped_native: str | None
    explorer: str  # "routescan" | "etherscan"
    explorer_rps: float
    rpc_url: str | None
    rpc_rps: float
    atomic_reconcile: bool = False  # Avalanche: detect C<->X/P atomic imports/exports
    price_chain: str = ""  # DefiLlama coin prefix, e.g. "avax"
    native_price_id: str = ""  # e.g. "coingecko:avalanche-2"
    rpc_batch: int = 20  # max JSON-RPC calls per batch (free RPC tiers cap this)
    dust_native: str = "0"  # unsolicited native inflows at or below this are spam dust (0 = off)
    history_start: str = "2020-01-01"  # exchanges: earliest day to read
    export: str = ""  # file-based exchanges: export file or folder
    parser: str = ""  # file-based exchanges: parser name (mexc | gemini)
    tz_hours: float = 0.0  # file-based exchanges: hours to subtract to get UTC
    deposit_addresses: tuple[str, ...] = ()  # exchanges: the user's own deposit addresses there
    credit_subtype: str = ""  # exchanges: what bare credits are (user-confirmed), e.g. staking_reward
    beacon_withdrawals: bool = False  # Ethereum: fetch consensus-layer withdrawals (Etherscan only)

    @property
    def explorer_url(self) -> str:
        if self.explorer == "routescan":
            return ROUTESCAN_URL.format(chain_id=self.chain_id)
        if self.explorer == "etherscan":
            return ETHERSCAN_URL
        if self.explorer == "koios":
            return KOIOS_URL
        if self.explorer in ("mempool", "blockcypher", "kaspa"):
            return self.explorer
        raise ConfigError(f"unknown explorer {self.explorer!r} for chain {self.name}")

    def explorer_api_key(self) -> str | None:
        if self.explorer == "etherscan":
            key = os.environ.get("ETHERSCAN_API_KEY") or None
            if key is None:
                raise ConfigError(
                    f"{self.name} uses the Etherscan API but ETHERSCAN_API_KEY is not set (.env)"
                )
            return key
        if self.explorer == "koios":
            return os.environ.get("KOIOS_API_KEY") or None
        return os.environ.get("ROUTESCAN_API_KEY") or None


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    chains: dict[str, ChainConfig]
    matching: dict[str, Any] = field(default_factory=dict)

    def resolve_chain(self, value: str) -> ChainConfig:
        v = value.strip().lower()
        for chain in self.chains.values():
            if v == chain.name.lower() or v in chain.aliases or v == str(chain.chain_id):
                return chain
        raise ConfigError(f"unknown chain {value!r}; known: {', '.join(self.chains)}")


def _chain_from_dict(name: str, d: dict[str, Any]) -> ChainConfig:
    return ChainConfig(
        name=name,
        kind=str(d["kind"]),
        aliases=tuple(str(a).lower() for a in d.get("aliases", [])),
        chain_id=int(d["chain_id"]) if d.get("chain_id") is not None else None,
        native_symbol=str(d["native_symbol"]),
        native_decimals=int(d.get("native_decimals", 18)),
        wrapped_native=str(d["wrapped_native"]).lower() if d.get("wrapped_native") else None,
        explorer=str(d.get("explorer", "")),
        explorer_rps=float(d.get("explorer_rps", 2)),
        rpc_url=d.get("rpc_url"),
        rpc_rps=float(d.get("rpc_rps", 4)),
        atomic_reconcile=bool(d.get("atomic_reconcile", False)),
        price_chain=str(d.get("price_chain", "")),
        native_price_id=str(d.get("native_price_id", "")),
        beacon_withdrawals=bool(d.get("beacon_withdrawals", False)),
        history_start=str(d.get("history_start", "2020-01-01")),
        export=str(d.get("export", "")),
        parser=str(d.get("parser", "")),
        tz_hours=float(d.get("tz_hours", 0) or 0),
        deposit_addresses=tuple(str(a).lower() for a in d.get("deposit_addresses") or []),
        credit_subtype=str(d.get("credit_subtype", "")),
        rpc_batch=int(d.get("rpc_batch", 20)),
        dust_native=str(d.get("dust_native", "0")),
    )


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    if path is None:
        text = resources.files("txclass").joinpath("settings.yaml").read_text()
    else:
        text = path.read_text()
    raw = yaml.safe_load(text)
    chains = {name: _chain_from_dict(name, d) for name, d in raw["chains"].items()}
    private = Path("exchange_deposits.yaml")  # git-ignored: the user's own deposit addresses per exchange
    if private.exists():
        from dataclasses import replace

        for name, addrs in (yaml.safe_load(private.read_text()) or {}).items():
            if name in chains:
                extra = tuple(str(a).lower() for a in addrs or [])
                chains[name] = replace(chains[name], deposit_addresses=chains[name].deposit_addresses + extra)
    return Settings(db_path=Path(raw.get("db_path", "data/txclass.sqlite")), chains=chains,
                    matching=dict(raw.get("matching") or {}))


@dataclass(frozen=True, slots=True)
class WalletSpec:
    address: str  # lowercased for EVM
    chain: ChainConfig
    label: str
    gift: bool = False  # label says "Gift": a recipient of the user's gifts, not one of the user's wallets


GIFT_LABEL = re.compile(r"\bgift\b", re.IGNORECASE)


def split_gifts(ws: list[WalletSpec]) -> tuple[list[WalletSpec], list[WalletSpec]]:
    """(own wallets, gift recipients). Gift recipients are never fetched or counted as the user's funds;
    sends to them are classified as gifts."""
    return [w for w in ws if not w.gift], [w for w in ws if w.gift]


def load_wallets(path: Path, settings: Settings, chain_override: str | None = None) -> list[WalletSpec]:
    """Read addresses.csv. Columns (case-insensitive): address (required), chain, label."""
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ConfigError(f"{path} is empty")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        if "address" not in cols:
            raise ConfigError(f"{path} needs an 'Address' column; found {reader.fieldnames}")
        wallets: list[WalletSpec] = []
        seen: set[tuple[str, str]] = set()
        unsupported: Counter[str] = Counter()
        for row in reader:
            addr = (row.get(cols["address"]) or "").strip()
            if not addr:
                continue
            chain_name = chain_override or (row.get(cols["chain"], "") if "chain" in cols else "")
            if not chain_name:
                raise ConfigError(f"no chain for {addr}: add a Chain column or pass --chain")
            try:
                chain = settings.resolve_chain(chain_name)
            except ConfigError:
                unsupported[chain_name.strip()] += 1  # e.g. Kaspa: no adapter yet; skip, don't abort
                continue
            if chain.kind == "evm":
                if not (addr.startswith("0x") and len(addr) == 42):
                    raise ConfigError(f"{addr!r} is not an EVM address (chain {chain.name})")
                addr = addr.lower()
            elif chain.kind == "injective" and addr.startswith("0x") and len(addr) == 42:
                addr = evm_to_bech32(addr, "inj")  # same key: Injective accounts are Ethereum-style (eth_secp256k1)
            elif chain.kind == "cardano":
                if not addr.startswith(("addr1", "stake1")):
                    raise ConfigError(f"{addr!r} is not a mainnet Cardano addr1/stake1 address")
            label = (row.get(cols["label"], "") if "label" in cols else "").strip()
            if (chain.name, addr) in seen:
                continue
            seen.add((chain.name, addr))
            wallets.append(WalletSpec(address=addr, chain=chain, label=label, gift=bool(GIFT_LABEL.search(label))))
    for name, n in unsupported.items():
        log.warning("skipping %d row(s) for chain %r: no adapter (supported: %s)", n, name, ", ".join(settings.chains))
    return wallets


_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def evm_to_bech32(addr: str, hrp: str) -> str:
    """0x… account bytes -> bech32 (BIP-173) address with the given prefix, e.g. Injective's inj1…."""
    acc = bits = 0
    data: list[int] = []
    for b in bytes.fromhex(addr[2:]):
        acc, bits = (acc << 8) | b, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    values = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp] + data + [0] * 6
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= g if (top >> i) & 1 else 0
    chk ^= 1
    return hrp + "1" + "".join(_BECH32[d] for d in data + [(chk >> 5 * (5 - i)) & 31 for i in range(6)])
