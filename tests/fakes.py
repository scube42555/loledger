"""In-process fakes for the explorer and JSON-RPC (no network in tests)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from loledger.config import ChainConfig
from loledger.http import HttpClient

CHAIN = ChainConfig(
    name="Avalanche", kind="evm", aliases=("avax",), chain_id=43114, native_symbol="AVAX",
    native_decimals=18, wrapped_native="0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",
    explorer="routescan", explorer_rps=0, rpc_url="http://rpc.test", rpc_rps=0,
)

W = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
TOKEN = "0x" + "cc" * 20
ROUTER = "0x" + "dd" * 20


class FakeExplorer:
    """Mimics Etherscan account endpoints: startblock/endblock/page/offset over block-sorted rows."""

    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data
        self.calls: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.calls.append(q)
        if q.get("action") == "getsourcecode":
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": [{"ContractName": "Router"}]})
        rows = [r for r in self.data.get(q["action"], [])
                if int(q["startblock"]) <= int(r["blockNumber"]) <= int(q["endblock"])]
        page, offset = int(q["page"]), int(q["offset"])
        chunk = rows[(page - 1) * offset: page * offset]
        if not chunk:
            return httpx.Response(200, json={"status": "0", "message": "No transactions found", "result": []})
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": chunk})

    def http(self) -> HttpClient:
        return HttpClient(0, client=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda s: None)


class FakeRpc:
    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        self.results = results or {}  # method -> key -> result
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        reqs = body if isinstance(body, list) else [body]
        out = []
        for r in reqs:
            key = r["params"][0]
            res = self.results.get(r["method"], {}).get(key)
            if r["method"] == "eth_getCode" and res is None:
                res = "0x"
            out.append({"jsonrpc": "2.0", "id": r["id"], "result": res})
        return httpx.Response(200, json=out if isinstance(body, list) else out[0])

    def http(self) -> HttpClient:
        return HttpClient(0, client=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda s: None)


def tx_row(h: str, block: int, frm: str, to: str, value: int = 0, *, is_error: str = "0",
           gas_used: int = 21000, gas_price: int = 25 * 10**9, method: str = "0x") -> dict[str, Any]:
    return {"blockNumber": str(block), "timeStamp": str(1_700_000_000 + block), "hash": h, "from": frm,
            "to": to, "value": str(value), "gasUsed": str(gas_used), "gasPrice": str(gas_price),
            "isError": is_error, "txreceipt_status": "0" if is_error == "1" else "1", "methodId": method,
            "functionName": "", "input": method, "contractAddress": ""}


def token_row(h: str, block: int, frm: str, to: str, value: int, *, token: str = TOKEN,
              decimals: int = 6, symbol: str = "USDC") -> dict[str, Any]:
    return {"blockNumber": str(block), "timeStamp": str(1_700_000_000 + block), "hash": h, "from": frm,
            "to": to, "value": str(value), "contractAddress": token, "tokenSymbol": symbol,
            "tokenDecimal": str(decimals)}


def transfer_log(token: str, frm: str, to: str, value: int, idx: int) -> dict[str, Any]:
    def pad(a: str) -> str:
        return "0x" + a[2:].rjust(64, "0")

    return {"address": token, "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                         pad(frm), pad(to)], "data": hex(value), "logIndex": hex(idx)}
