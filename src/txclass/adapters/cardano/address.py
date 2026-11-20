"""Minimal Shelley address decoding (bech32 header only): enough to tell key vs script payment parts."""

from __future__ import annotations

from dataclasses import dataclass

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_bytes(s: str) -> bytes:
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1:
        raise ValueError(f"not bech32: {s[:20]}")
    data = [CHARSET.find(c) for c in s[pos + 1:-6]]
    if any(d < 0 for d in data):
        raise ValueError(f"not bech32: {s[:20]}")
    acc, bits, out = 0, 0, bytearray()
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


@dataclass(frozen=True, slots=True)
class ShelleyAddress:
    bech32: str
    addr_type: int  # CIP-19 header type 0..7 (base/pointer/enterprise), 14/15 reward

    @property
    def is_script_payment(self) -> bool:
        return self.addr_type in (1, 3, 5, 7)

    @property
    def is_key_payment(self) -> bool:
        return self.addr_type in (0, 2, 4, 6)

    @property
    def payment_cred(self) -> str:
        return _bech32_bytes(self.bech32)[1:29].hex()


def parse_address(bech32: str) -> ShelleyAddress | None:
    """None for Byron (Ae2…/DdzFF…) or malformed addresses."""
    if not bech32.startswith(("addr1", "addr_test1", "stake1")):
        return None
    try:
        header = _bech32_bytes(bech32)[0]
    except ValueError:
        return None
    return ShelleyAddress(bech32=bech32, addr_type=header >> 4)
