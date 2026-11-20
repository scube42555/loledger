"""Contract/token registry per chain (YAML), merged with verified names discovered while fetching."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from importlib import resources

import yaml

ZERO = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class ContractInfo:
    name: str
    kind: str
    protocol: str


@dataclass
class Registry:
    tokens: dict[str, str] = field(default_factory=dict)  # canonical address -> symbol
    contracts: dict[str, ContractInfo] = field(default_factory=dict)
    discovered: dict[str, tuple[bool | None, str]] = field(default_factory=dict)  # addr -> (is_contract, name)
    price_aliases: dict[str, str] = field(default_factory=dict)  # token -> fallback price id

    @property
    def canonical_symbols(self) -> set[str]:
        return {norm_symbol(s) for s in self.tokens.values()}

    def contract(self, addr: str) -> ContractInfo | None:
        return self.contracts.get(addr)

    def is_contract(self, addr: str) -> bool | None:
        if addr in self.contracts or addr in self.tokens:
            return True
        return self.discovered.get(addr, (None, ""))[0]

    def label(self, addr: str) -> str:
        c = self.contracts.get(addr)
        if c:
            return c.protocol
        name = self.discovered.get(addr, (None, ""))[1]
        return name or ""


def norm_symbol(sym: str) -> str:
    """Case/compat-folded symbol with bridge suffixes dropped: 'USDT.e' -> 'USDT'."""
    s = unicodedata.normalize("NFKC", sym).casefold().strip()
    for suf in (".e", ".b"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def load_registry(chain_name: str, discovered: dict[str, tuple[bool | None, str]] | None = None) -> Registry:
    base = resources.files("txclass.registry")
    ref = next((base.joinpath(f) for f in (f"evm_{chain_name.lower()}.yaml", f"{chain_name.lower()}.yaml")
                if base.joinpath(f).is_file()), None)
    reg = Registry(discovered=dict(discovered or {}))
    if ref is None or chain_name.lower() == "cardano":  # cardano.yaml has its own schema (scripts)
        return reg
    raw = yaml.safe_load(ref.read_text()) or {}
    def key(a: str) -> str:
        """Lowercase hex addresses only: base58 keys (Solana) and Move type names after '::' (Sui) are case-sensitive."""
        if not a.startswith("0x"):
            return a
        addr, sep, rest = a.partition("::")
        return addr.lower() + sep + rest

    reg.tokens = {key(a): str(s) for a, s in (raw.get("tokens") or {}).items()}
    reg.price_aliases = {key(a): str(c) for a, c in (raw.get("price_aliases") or {}).items()}
    reg.contracts = {
        key(a): ContractInfo(name=str(c.get("name", "")), kind=str(c["kind"]), protocol=str(c.get("protocol", "")))
        for a, c in (raw.get("contracts") or {}).items()
    }
    return reg
