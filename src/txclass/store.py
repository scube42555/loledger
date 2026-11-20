"""SQLite store: raw API response cache plus normalized tables."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .model import AssetKind, Log, Movement, TxContext

# Never part of a cache key, never stored.
SECRET_PARAMS = frozenset({"apikey", "api_key", "project_id"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_responses (
    source TEXT NOT NULL, endpoint TEXT NOT NULL, params TEXT NOT NULL,
    body TEXT NOT NULL, fetched_at INTEGER NOT NULL, is_tail INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, endpoint, params)
);
CREATE TABLE IF NOT EXISTS address_info (
    chain TEXT NOT NULL, address TEXT NOT NULL,
    is_contract INTEGER, contract_name TEXT,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS movements (
    chain TEXT NOT NULL, wallet TEXT NOT NULL, tx_hash TEXT NOT NULL, leg_id TEXT NOT NULL,
    block INTEGER NOT NULL, timestamp INTEGER NOT NULL,
    asset_id TEXT NOT NULL, asset_kind TEXT NOT NULL, symbol TEXT NOT NULL, decimals INTEGER NOT NULL,
    amount TEXT NOT NULL, counterparty TEXT NOT NULL, source TEXT NOT NULL,
    utxo_ref TEXT NOT NULL DEFAULT '', datum_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chain, wallet, tx_hash, leg_id)
);
CREATE TABLE IF NOT EXISTS txs (
    chain TEXT NOT NULL, tx_hash TEXT NOT NULL, block INTEGER NOT NULL, timestamp INTEGER NOT NULL,
    tx_from TEXT NOT NULL, tx_to TEXT NOT NULL, method_id TEXT NOT NULL, function_name TEXT NOT NULL,
    is_error INTEGER NOT NULL, gas_fee TEXT NOT NULL, value TEXT NOT NULL, input TEXT NOT NULL,
    logs TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain, tx_hash)
);
"""


def canonical_params(params: Mapping[str, Any]) -> str:
    clean = {k: params[k] for k in sorted(params) if k.lower() not in SECRET_PARAMS}
    return json.dumps(clean, separators=(",", ":"), sort_keys=True, default=str)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(txs)")}
        if "meta" not in cols:  # migrate databases created before Cardano support
            self.conn.execute("ALTER TABLE txs ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
        self.stats = CacheStats()

    def close(self) -> None:
        self.conn.close()

    # ---- raw cache -------------------------------------------------------------------------
    def get_raw(self, source: str, endpoint: str, params: Mapping[str, Any], *,
                allow_tail: bool = True) -> Any | None:
        row = self.conn.execute(
            "SELECT body, is_tail FROM raw_responses WHERE source=? AND endpoint=? AND params=?",
            (source, endpoint, canonical_params(params)),
        ).fetchone()
        if row is None or (row[1] and not allow_tail):
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return json.loads(row[0])

    def put_raw(self, source: str, endpoint: str, params: Mapping[str, Any], body: Any, *,
                is_tail: bool = False) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_responses VALUES (?,?,?,?,?,?)",
            (source, endpoint, canonical_params(params), json.dumps(body), int(time.time()), int(is_tail)),
        )
        self.conn.commit()

    # ---- address info ----------------------------------------------------------------------
    def get_address_info(self, chain: str) -> dict[str, tuple[bool | None, str]]:
        rows = self.conn.execute(
            "SELECT address, is_contract, contract_name FROM address_info WHERE chain=?", (chain,)
        )
        return {a: (None if c is None else bool(c), n or "") for a, c, n in rows}

    def set_address_info(self, chain: str, address: str, *, is_contract: bool | None = None,
                         contract_name: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO address_info (chain, address, is_contract, contract_name) VALUES (?,?,?,?) "
            "ON CONFLICT(chain, address) DO UPDATE SET "
            "is_contract=COALESCE(excluded.is_contract, is_contract), "
            "contract_name=COALESCE(excluded.contract_name, contract_name)",
            (chain, address, None if is_contract is None else int(is_contract), contract_name),
        )

    def commit(self) -> None:
        self.conn.commit()

    # ---- normalized tables -----------------------------------------------------------------
    def replace_wallet_movements(self, chain: str, wallet: str, movements: Iterable[Movement]) -> None:
        self.conn.execute("DELETE FROM movements WHERE chain=? AND wallet=?", (chain, wallet))
        self.conn.executemany(
            "INSERT INTO movements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (m.chain, m.wallet, m.tx_hash, m.leg_id, m.block, m.timestamp, m.asset_id,
                 m.asset_kind.value, m.symbol, m.decimals, str(m.amount), m.counterparty, m.source,
                 m.utxo_ref, m.datum_hash)
                for m in movements
            ],
        )
        self.conn.commit()

    def upsert_txs(self, txs: Iterable[TxContext]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO txs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (t.chain, t.tx_hash, t.block, t.timestamp, t.tx_from, t.tx_to, t.method_id,
                 t.function_name, int(t.is_error), str(t.gas_fee), str(t.value), t.input,
                 json.dumps([[lg.address, list(lg.topics), lg.data, lg.log_index] for lg in t.logs]),
                 json.dumps(t.meta, default=str))
                for t in txs
            ],
        )
        self.conn.commit()

    def load_movements(self, chain: str | None = None, wallet: str | None = None) -> list[Movement]:
        q = "SELECT * FROM movements WHERE 1=1"
        args: list[str] = []
        if chain:
            q += " AND chain=?"
            args.append(chain)
        if wallet:
            q += " AND wallet=?"
            args.append(wallet)
        q += " ORDER BY timestamp, tx_hash, leg_id"
        out = []
        for r in self.conn.execute(q, args):
            (ch, w, h, leg, blk, ts, aid, kind, sym, dec, amt, cp, src, uref, dh) = r
            out.append(Movement(chain=ch, tx_hash=h, leg_id=leg, block=blk, timestamp=ts, wallet=w,
                                asset_id=aid, asset_kind=AssetKind(kind), symbol=sym, decimals=dec,
                                amount=Decimal(amt), counterparty=cp, source=src, utxo_ref=uref,
                                datum_hash=dh))
        return out

    def load_txs(self, chain: str) -> dict[str, TxContext]:
        out: dict[str, TxContext] = {}
        for r in self.conn.execute("SELECT * FROM txs WHERE chain=?", (chain,)):
            (ch, h, blk, ts, frm, to, mid, fn, err, gas, val, inp, logs, meta) = r
            out[h] = TxContext(
                chain=ch, tx_hash=h, block=blk, timestamp=ts, tx_from=frm, tx_to=to, method_id=mid,
                function_name=fn, is_error=bool(err), gas_fee=Decimal(gas), value=Decimal(val),
                input=inp,
                logs=[Log(address=a, topics=tuple(t), data=d, log_index=i) for a, t, d, i in json.loads(logs)],
                meta=json.loads(meta),
            )
        return out
