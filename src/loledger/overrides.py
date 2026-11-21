"""User-confirmed classification overrides (overrides.yaml in the project root)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .rules import Classified, Cls


def load_overrides(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a list of rules")
    return raw


def _hash(h: Any) -> str:
    """YAML reads an unquoted 0x... hash as an int: turn it back into the 32-byte hex string."""
    return f"0x{h:064x}" if isinstance(h, int) else str(h).lower()


def apply_overrides(rows: list[Classified], rules: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for i, rule in enumerate(rules):
        hashes = {_hash(h) for h in (rule.get("tx_hashes") or [])}
        if rule.get("tx_hash"):
            hashes.add(_hash(rule["tx_hash"]))
        cp, wallet = rule.get("counterparty"), rule.get("wallet")
        cls = Cls(rule["classification"])
        n = 0
        for r in rows:
            m = r.movement
            if rule.get("chain") and m.chain != rule["chain"]:
                continue
            if hashes and m.tx_hash.lower() not in hashes:
                continue
            if cp and m.counterparty != cp:
                continue
            if wallet and m.wallet != wallet:
                continue
            if rule.get("direction") == "out" and m.amount >= 0 or rule.get("direction") == "in" and m.amount <= 0:
                continue
            if rule.get("only_classification") and r.classification.value != rule["only_classification"]:
                continue
            if rule.get("token") and m.symbol != rule["token"]:
                continue
            if not (hashes or cp or wallet):
                continue  # a rule must name something specific
            r.classification, r.subtype, r.confidence = cls, str(rule.get("subtype", "")), 1.0
            r.reason = f"user-confirmed: {rule.get('reason', '')}"
            for key, attr in (("linked_tx_hash", "linked_tx_hash"), ("order_id", "order_id"), ("leg_role", "leg_role")):
                if rule.get(key) is not None:
                    v = rule[key]
                    setattr(r, attr, f"0x{v:064x}" if isinstance(v, int) else str(v))
            n += 1
        notes.append(f"override {i + 1} ({rule.get('subtype') or cls.value}): {n} row(s)")
    return notes


def gift_rules(gifts: list[Any]) -> list[dict[str, Any]]:
    """One rule per gift recipient listed in addresses.csv: everything the user's wallets sent there is a gift."""
    return [{"counterparty": g.address, "direction": "out", "classification": "OTHER", "subtype": "gift_sent",
             "reason": f"gift to {g.label} (recipient listed as a Gift in addresses.csv, not the user's wallet)"}
            for g in gifts]
