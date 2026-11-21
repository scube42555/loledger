"""Cosmos SDK GetTx responses (Cosmos Hub, dYdX) -> the Cosmos SDK normalizer shared with Injective.

Two result layouts occur on the archive node:
- SDK ≤ 0.47 (e.g. April 2024): `tx_response.logs` holds one entry per message, each with its own events;
- SDK 0.50 (e.g. from late 2024): `logs` is empty and `tx_response.events` is flat. Message events carry a
  `msg_index` attribute; events without one belong to the ante handler (fee payment, signatures) and are
  dropped here. The fee is booked from `auth_info.fee` instead.
The fee payer is `auth_info.fee.payer` or else the first signer, read from the first message.

IBC receipts (MsgRecvPacket relayed by a third party) are credited by the transfer escrow account. The
counterparty is rewritten to the sending address on the other chain (`ibc:<sender>`), taken from the
`fungible_token_packet` event, so the report shows where the coins came from. The tx memo of a relayed receipt
is the relayer's advertisement, so it is kept as `relayer_memo`, not as the `note` the spam rule reads.

dYdX bridge credits (Ethereum DYDX migrated to the chain) have no tx: they are added from `credits` as
`bridge:<event id>` with source `bridge_mint`, at the completion block's time.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale
from ..injective.normalize import _ts
from ..injective.normalize import normalize as normalize_sdk

_SIGNER_FIELDS = ("from_address", "delegator_address", "sender", "signer", "granter", "voter", "depositor")


def _signer(msg: dict[str, Any]) -> str:
    for f in _SIGNER_FIELDS:
        if isinstance(msg.get(f), str) and msg[f]:
            return str(msg[f])
    inputs = msg.get("inputs") or []
    return str(inputs[0].get("address", "")) if inputs else ""


def _logs(resp: dict[str, Any], n_msgs: int) -> list[dict[str, Any]]:
    if resp.get("logs"):
        return [{"events": lg.get("events") or []} for lg in resp["logs"]]
    per: list[list[dict[str, Any]]] = [[] for _ in range(n_msgs)]
    for e in resp.get("events") or []:
        attrs = e.get("attributes") or []
        idx = next((a.get("value") for a in attrs if a.get("key") == "msg_index"), None)
        if idx is None or not str(idx).isdigit() or int(idx) >= n_msgs:
            continue
        per[int(idx)].append({"type": e["type"], "attributes": [a for a in attrs if a.get("key") != "msg_index"]})
    return [{"events": evs} for evs in per]


def to_history(t: dict[str, Any]) -> dict[str, Any]:
    resp, body, auth = t["tx_response"], t["tx"]["body"], t["tx"]["auth_info"]
    msgs = body.get("messages") or []
    fee = auth.get("fee") or {}
    return {"hash": resp["txhash"], "block_number": resp["height"], "block_timestamp": resp["timestamp"],
            "code": resp.get("code") or 0,
            "gas_fee": {"payer": fee.get("payer") or (_signer(msgs[0]) if msgs else ""), "amount": fee.get("amount") or []},
            "messages": [{"type": m.get("@type", ""), "value": m} for m in msgs],
            "logs": _logs(resp, len(msgs)), "memo": body.get("memo") or ""}


def _ibc_senders(h: dict[str, Any], wallet: str) -> list[tuple[str, str]]:
    """(sender on the other chain, raw amount) of IBC transfers this tx delivered to the wallet."""
    out = []
    for lg in h["logs"]:
        for e in lg["events"]:
            if e["type"] == "fungible_token_packet":
                a = {x["key"]: x["value"] for x in e["attributes"]}
                if a.get("receiver") == wallet and a.get("success", "true") == "true":
                    out.append((str(a.get("sender", "")), str(a.get("amount", ""))))
    return out


def normalize(chain: ChainConfig, wallet: str, txs: list[dict[str, Any]], modules: dict[str, str],
              denom_meta: Any, credits: list[dict[str, Any]] | None = None,
              now: int | None = None) -> tuple[list[Movement], dict[str, TxContext]]:
    """credits: dYdX bridge credits from CosmosClient.bridge_credits (no tx of their own)."""
    from .fetch import PROFILES

    p = PROFILES[chain.name]
    hist = [to_history(t) for t in txs]
    moves, ctx = normalize_sdk(chain, wallet, hist, modules, denom_meta, native_denom=p.native_denom, slug=p.slug,
                               leg_prefix=p.leg_prefix, now=now)
    by_hash = {h["hash"]: h for h in hist}
    out: list[Movement] = []
    for m in moves:
        h = by_hash.get(m.tx_hash)
        if h is not None and m.amount > 0:
            senders = _ibc_senders(h, wallet)
            if senders:
                raw = str(int(m.amount.scaleb(m.decimals)))
                sender = next((s for s, a in senders if a == raw), senders[0][0])
                m = dataclasses.replace(m, counterparty=f"ibc:{sender}", source="ibc_receive")
        out.append(m)
    for h in hist:
        if h["hash"] in ctx:
            senders = _ibc_senders(h, wallet)
            if senders:  # the memo is the relayer's ("Relayed by … https://hermes…"), not the sender's: not a lure
                ctx[h["hash"]].meta["ibc_senders"] = [s for s, _ in senders]
                ctx[h["hash"]].meta["relayer_memo"] = h["memo"]
            else:
                ctx[h["hash"]].meta["note"] = h["memo"]
    for c in credits or []:
        ts = _ts(str(c["time"]))
        if now is not None and ts > now:
            continue
        native = c["denom"] == p.native_denom
        sym, dec = (chain.native_symbol, chain.native_decimals) if native else denom_meta(c["denom"])
        cid = f"bridge:{c['id']}"
        out.append(Movement(chain=chain.name, tx_hash=cid, leg_id="bridge_credit", block=int(c["height"]), timestamp=ts,
                            wallet=wallet, asset_id=NATIVE_ASSET if native else c["denom"],
                            asset_kind=AssetKind.NATIVE if native else AssetKind.ERC20, symbol=sym, decimals=dec,
                            amount=scale(int(c["amount"]), dec), counterparty=f"{p.slug}:bridge", source="bridge_mint"))
        ctx[cid] = TxContext(chain=chain.name, tx_hash=cid, block=int(c["height"]), timestamp=ts, tx_from="",
                           tx_to=f"{p.slug}:bridge", method_id="", function_name="bridge_deposit", is_error=False,
                           gas_fee=Decimal(0), value=Decimal(0),
                           meta={"bridge_event_id": c["id"], "eth_block": c.get("eth_block", ""), "ack_tx": c["ack_tx"]})
    out.sort(key=lambda m: (m.timestamp, m.tx_hash, m.leg_id))
    return out, ctx
