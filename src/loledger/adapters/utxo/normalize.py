"""UTXO txs -> wallet-perspective Movements (one address per wallet row).

Wallet-sent tx (the wallet funds an input): one leg per output paid elsewhere (counterparty = that
address, leg_id vout:N). Change back to the wallet is not a leg; the fee is the wallet's gas when it
funded every input. Received tx: one leg per output paid to the wallet; counterparty = first input.
Reconciles: wallet_out - wallet_in == sum(legs) - fee.
"""

from __future__ import annotations

from decimal import Decimal

from ...config import ChainConfig
from ...model import NATIVE_ASSET, AssetKind, Movement, TxContext, scale
from .fetch import UTx


def normalize(chain: ChainConfig, wallet: str, history: list[UTx]) -> tuple[list[Movement], dict[str, TxContext]]:
    dec = chain.native_decimals
    moves: list[Movement] = []
    txs: dict[str, TxContext] = {}
    for t in history:
        w_in = sum(i["value"] for i in t["inputs"] if i["addr"] == wallet)
        w_out = sum(o["value"] for o in t["outputs"] if o["addr"] == wallet)
        total_in = sum(i["value"] for i in t["inputs"])
        total_out = sum(o["value"] for o in t["outputs"])
        fee = t["fee"] if t.get("fee") is not None else max(0, total_in - total_out)
        all_ours = w_in > 0 and all(i["addr"] == wallet for i in t["inputs"])
        first_in = next((i["addr"] for i in t["inputs"] if i["addr"] != wallet), "") or (t["inputs"][0]["addr"] if t["inputs"] else "")

        def mk(n: int | str, raw: int, cp: str) -> Movement:
            return Movement(chain=chain.name, tx_hash=t["txid"], leg_id=f"vout:{n}", block=int(t["height"]),
                            timestamp=int(t["ts"]), wallet=wallet, asset_id=NATIVE_ASSET, asset_kind=AssetKind.NATIVE,
                            symbol=chain.native_symbol, decimals=dec, amount=scale(raw, dec), counterparty=cp,
                            source="utxo")

        legs: list[Movement] = []
        if all_ours:
            legs = [mk(o["n"], -o["value"], o["addr"] or "unknown") for o in t["outputs"] if o["addr"] != wallet and o["value"]]
        elif w_in > 0:  # shared inputs (coinjoin/payjoin): one net leg, fee not attributed
            net = w_out - w_in
            if net:
                legs = [mk("net", net, first_in)]
        else:
            legs = [mk(o["n"], o["value"], first_in or "coinbase") for o in t["outputs"] if o["addr"] == wallet and o["value"]]
        if not legs and not w_in:
            continue
        moves.extend(legs)
        # wallet-independent (the tx is shared by every listed wallet it touches): the sender is the
        # address funding the inputs and pays the whole fee
        sender = t["inputs"][0]["addr"] if t["inputs"] else ""
        single_sender = bool(t["inputs"]) and all(i["addr"] == sender for i in t["inputs"])
        txs[t["txid"]] = TxContext(
            chain=chain.name, tx_hash=t["txid"], block=int(t["height"]), timestamp=int(t["ts"]),
            tx_from=sender, tx_to=next((o["addr"] for o in t["outputs"] if o["addr"] != sender), sender),
            method_id="", function_name="" if legs else "consolidation", is_error=False,
            gas_fee=scale(fee, dec) if single_sender else Decimal(0), value=Decimal(0))
    return moves, txs
