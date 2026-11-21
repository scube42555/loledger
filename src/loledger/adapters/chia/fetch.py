"""Chia via coinset.org (public full-node RPC, keyless). Immutable lookups are cached."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ...config import ChainConfig
from ...http import HttpClient, TransientError
from ...store import Store

log = logging.getLogger(__name__)
BASE = "https://api.coinset.org"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _check(body: Any) -> None:
    if isinstance(body, dict) and body.get("success") is False:
        err = str(body.get("error"))
        if "rate" in err.lower() or "too many" in err.lower():
            raise TransientError(err)
        raise RuntimeError(f"coinset error: {err[:200]}")


def _int_bytes(v: int) -> bytes:
    """clvm/Chia minimal two's-complement encoding used in coin ids."""
    if v == 0:
        return b""
    b = v.to_bytes((v.bit_length() + 8) // 8, "big", signed=True)
    while len(b) > 1 and b[0] == 0 and b[1] < 0x80:
        b = b[1:]
    return b


def coin_id(parent: str, puzzle_hash: str, amount: int) -> str:
    return "0x" + hashlib.sha256(bytes.fromhex(parent[2:]) + bytes.fromhex(puzzle_hash[2:]) + _int_bytes(amount)).hexdigest()


def _bech32_polymod(v: list[int]) -> int:
    g = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    c = 1
    for x in v:
        b = c >> 25
        c = (c & 0x1FFFFFF) << 5 ^ x
        for i in range(5):
            c ^= g[i] if (b >> i) & 1 else 0
    return c


def _conv(data: bytes, frm: int, to: int, pad: bool = True) -> list[int]:
    acc = bits = 0
    out = []
    for b in data:
        acc = (acc << frm) | b
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & ((1 << to) - 1))
    if pad and bits:
        out.append((acc << (to - bits)) & ((1 << to) - 1))
    return out


def puzzle_hash_to_address(ph: str, hrp: str = "xch") -> str:
    d = _conv(bytes.fromhex(ph[2:] if ph.startswith("0x") else ph), 8, 5)
    exp = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    pm = _bech32_polymod(exp + d + [0] * 6) ^ 0x2BC830A3
    return hrp + "1" + "".join(CHARSET[x] for x in d + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def address_to_puzzle_hash(addr: str) -> str:
    data = [CHARSET.find(c) for c in addr[addr.rfind("1") + 1:-6]]
    return "0x" + bytes(_conv(bytes(data), 5, 8, pad=False)).hex()


class ChiaClient:
    def __init__(self, chain: ChainConfig, store: Store, http: HttpClient | None = None) -> None:
        self.store = store
        self.http = http or HttpClient(chain.rpc_rps or 3)

    def post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(self.http.request_json("POST", f"{BASE}/{method}", json_body=body, check=_check))

    def _cached(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        c = self.store.get_raw("coinset", method, body)
        if c is None:
            c = self.post(method, body)
            self.store.put_raw("coinset", method, body, c)
        return dict(c)

    def coin_records(self, puzzle_hash: str) -> list[dict[str, Any]]:
        """Live (a new coin may arrive any time); not cached."""
        return list(self.post("get_coin_records_by_puzzle_hash",
                              {"puzzle_hash": puzzle_hash, "include_spent_coins": True}).get("coin_records") or [])

    def coin(self, name: str) -> dict[str, Any]:
        return dict(self._cached("get_coin_record_by_name", {"name": name}).get("coin_record") or {})

    def block(self, height: int) -> dict[str, Any]:
        return dict(self._cached("get_block_record_by_height", {"height": height}).get("block_record") or {})

    def additions_removals(self, header_hash: str) -> dict[str, Any]:
        return self._cached("get_additions_and_removals", {"header_hash": header_hash})
