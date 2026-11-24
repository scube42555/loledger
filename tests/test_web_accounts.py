"""Accounts page backend: reading/adding wallets in addresses.csv and the background sync runner."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from loledger.config import load_settings
from loledger.web import accounts as acc

S = load_settings(None)


def _csv(tmp_path: Path, body: str) -> Path:
    (tmp_path / "addresses.csv").write_text("Address,Chain,Label\n" + body)
    return tmp_path


def test_read_wallets_canonical_chain_gift_and_unsupported(tmp_path: Path) -> None:
    root = _csv(tmp_path, "0xAbC0000000000000000000000000000000000001,eth,Main\n"
                          "0xAbC0000000000000000000000000000000000002,Ethereum,Bob Gift\n"
                          "osmo1xyz,Osmosis,Osmo\n")
    ws = acc.read_wallets(root, S)
    assert [(w.chain, w.supported, w.gift) for w in ws] == [("Ethereum", True, False), ("Ethereum", True, True),
                                                           ("Osmosis", False, False)]
    assert ws[0].key == "Ethereum|0xabc0000000000000000000000000000000000001"


def test_validate_rejects_bad_duplicate_and_exchange(tmp_path: Path) -> None:
    root = _csv(tmp_path, "0xAbC0000000000000000000000000000000000001,Ethereum,Main\n")
    ws = acc.read_wallets(root, S)
    with pytest.raises(ValueError, match="42 characters"):
        acc.validate(S, "Ethereum", "0x12", ws)
    with pytest.raises(ValueError, match="already listed"):
        acc.validate(S, "ethereum", "0xabc0000000000000000000000000000000000001", ws)
    with pytest.raises(ValueError, match="exchange"):
        acc.validate(S, "Gemini", "whatever", ws)
    with pytest.raises(ValueError, match="addr1"):
        acc.validate(S, "Cardano", "Ae2abc", ws)
    with pytest.raises(ValueError, match="commas"):
        acc.validate(S, "Osmosis", "osmo1,x", ws)
    assert acc.validate(S, "base", "0xAbC0000000000000000000000000000000000001", ws) == (
        "Base", "0xAbC0000000000000000000000000000000000001")  # same address on another chain is fine
    assert acc.validate(S, "Osmosis", " osmo1abc ", ws) == ("Osmosis", "osmo1abc")  # unsupported chains allowed


def test_append_wallet_keeps_file_valid_without_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "addresses.csv").write_text("Address,Chain,Label\nbc1qa,Bitcoin,Cold")
    acc.append_wallet(tmp_path, "Solana", "So1ana", 'My "hot", wallet')
    ws = acc.read_wallets(tmp_path, S)
    assert [(w.address, w.chain, w.label) for w in ws] == [("bc1qa", "Bitcoin", "Cold"),
                                                           ("So1ana", "Solana", 'My "hot", wallet')]


def test_syncer_runs_steps_logs_output_and_reloads_on_success(tmp_path: Path) -> None:
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True))
    s._cli = lambda *a: [sys.executable, "-c", "print('fetched', 3)", *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet("bc1qa", "Bitcoin", "Cold", True, False, "utxo"))
    with pytest.raises(RuntimeError):
        s.sync_all()  # one job at a time
    for _ in range(100):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 0 and reloaded == [True]
    assert "fetched 3" in s.job.log and s.job.log[-1] == "Done."
    assert s.job.log[0].endswith("--refresh --only bc1qa")  # only this wallet is fetched


def test_syncer_stops_at_failing_step_without_reloading(tmp_path: Path) -> None:
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True))
    s._cli = lambda *a: [sys.executable, "-c", "import sys; sys.exit(3)", *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet("bc1qa", "Bitcoin", "", True, False, "utxo"))
    for _ in range(100):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 3 and not reloaded
    assert sum(line.startswith("$ loledger") for line in s.job.log) == 1  # second step never ran


# -- counterpart accounts of new transfers ----------------------------------------------------------
A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
GIFT = "0x" + "9" * 40
WALLETS = [acc.Wallet(A, "Ethereum", "Main", True, False, "evm"), acc.Wallet(B, "Ethereum", "Cold", True, False, "evm"),
           acc.Wallet(C.upper().replace("0X", "0x"), "Ethereum", "Spare", True, False, "evm"),
           acc.Wallet(GIFT, "Ethereum", "Gift to Bob", True, True, "evm"),
           acc.Wallet("addr1qxyz", "Cardano", "Ada", True, False, "cardano")]
HEAD = ("chain,timestamp,date_utc,tx_hash,leg_id,wallet,wallet_label,token,amount,counterparty,classification,subtype,"
        "leg_role,linked_tx_hash,withdrawal_address\n")


def _row(chain: str, tx: str, wallet: str, amount: str, cp: str = "", cls: str = "TRANSFER", sub: str = "",
         linked: str = "", dest: str = "", label: str = "", leg: str = "value") -> str:
    return f"{chain},0,2026-11-20 10:00:00,{tx},{leg},{wallet},{label},ETH,{amount},{cp},{cls},{sub},,{linked},{dest}\n"


def test_counterparts_of_new_transfers_whose_other_side_is_missing(tmp_path: Path) -> None:
    p = tmp_path / "classified.csv"
    old = _row("Ethereum", "0xold", A, "-1", cp=B, cls="INTERNAL_TRANSFER")
    p.write_text(HEAD + old)
    before = acc.leg_keys(p)
    p.write_text(HEAD + old
                 + _row("Ethereum", "0x1", A, "-1", cp=B, cls="INTERNAL_TRANSFER")       # B lacks 0x1 -> B
                 + _row("Ethereum", "0x2", A, "-1", cp=C, cls="INTERNAL_TRANSFER")       # C already has 0x2
                 + _row("Ethereum", "0x2", C, "1", cp=A, cls="INTERNAL_TRANSFER")
                 + _row("Ethereum", "0x3", A, "-1", cp="0xdead", sub="to_exchange:okx")  # deposit address -> OKX
                 + _row("Ethereum", "0x4", A, "-1", cp="0xbeef", sub="to_exchange:gemini", linked="gemini:dep:1")
                 + _row("Ethereum", "0x5", A, "-1", cp=GIFT, sub="gift_sent")            # gift recipient: never synced
                 + _row("Ethereum", "0x6", A, "-1", cp=B, sub="spam_phishing")
                 + _row("Ethereum", "0x7", A, "-1", cp="0xdead", sub="to_exchange:mexc")  # closed
                 + _row("Gemini", "gemini:wd:9", "gemini:exchange", "-2", cp="gemini", sub="withdrawal_to_chain",
                        dest=C.upper().replace("0X", "0x"))                              # exchange-reported destination
                 + _row("Cardano", "cx", "stake1abc", "5", cp="stake1zzz", label="Ada"))
    out = acc.counterparts(p, S, WALLETS, before, [A], closed=["MEXC"])
    assert list(out) == [B, "OKX", C.upper().replace("0X", "0x")]
    assert out["OKX"].startswith("2026-11-20: 1 ETH to OKX")
    # a counterparty that is a Cardano stake key resolves to the addresses.csv row listed by its addr1 address
    p.write_text(HEAD + _row("Cardano", "cy", "stake1other", "-5", cp="stake1abc", cls="INTERNAL_TRANSFER")
                 + _row("Cardano", "cz", "stake1abc", "1", label="Ada"))
    assert list(acc.counterparts(p, S, WALLETS, set(), ["stake1other"])) == ["addr1qxyz"]


def test_syncer_follows_up_with_counterpart_accounts(tmp_path: Path) -> None:
    (tmp_path / "addresses.csv").write_text(f"Address,Chain,Label\n{A},Ethereum,Main\n{B},Ethereum,Cold\n")
    (tmp_path / "classified.csv").write_text(HEAD)
    new = HEAD + _row("Ethereum", "0x1", A, "-1", cp=B, cls="INTERNAL_TRANSFER")
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True), S)
    script = f"import pathlib; p = pathlib.Path('classified.csv'); p.write_text({new!r}) if p.read_text() == {HEAD!r} else None"
    s._cli = lambda *a: [sys.executable, "-c", script, *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet(A, "Ethereum", "Main", True, False, "evm"))
    for _ in range(100):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 0 and reloaded == [True]
    cmds = [line for line in s.job.log if line.startswith("$ loledger")]
    assert len(cmds) == 2 and cmds[1].endswith(f"--refresh --only {B}")
    assert any("1 ETH to Cold" in line for line in s.job.log)


def test_counterparts_receipt_from_a_known_exchange_payout_address(tmp_path: Path) -> None:
    hot = "0x" + "5" * 40
    p = tmp_path / "classified.csv"
    old = _row("Ethereum", "0xw1", A, "1", cp=hot, cls="INTERNAL_TRANSFER", sub="from_exchange:gemini",
               linked="gemini:onchain_withdrawal:1")
    p.write_text(HEAD + old)
    before = acc.leg_keys(p)
    p.write_text(HEAD + old + _row("Ethereum", "0xw2", A, "2", cp=hot, sub="from_eoa")    # Gemini not synced yet
                 + _row("Ethereum", "0xw3", A, "-2", cp=hot, sub="to_eoa"))               # sends to it don't count
    out = acc.counterparts(p, S, WALLETS, before, [A])
    assert list(out) == ["Gemini"] and "2 ETH from Gemini (its payout address" in out["Gemini"]


def test_addresses_display_in_checksum_case_and_compare_case_insensitively() -> None:
    from loledger.config import display_address, norm_address

    typed = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"  # EIP-55 example
    assert display_address(typed.lower()) == typed  # EIP-55 restored from the internal lower-case form
    assert norm_address(typed) == typed.lower()
    sol = "ExampLeSo1anaDepositAddressMixedCase9xYz"
    assert display_address(sol) == norm_address(sol) == sol  # base58 is case-sensitive: untouched
    assert display_address("gemini") == "gemini"


def test_syncer_follows_the_chain_until_nothing_new(tmp_path: Path) -> None:
    """A's sync shows a transfer to B; B's sync shows one from C; C's sync shows one back to A (already synced)."""
    (tmp_path / "addresses.csv").write_text(f"Address,Chain,Label\n{A},Ethereum,Main\n{B},Ethereum,Cold\n"
                                            f"{C},Ethereum,Spare\n")
    (tmp_path / "classified.csv").write_text(HEAD)
    adds = {A: _row("Ethereum", "0x1", A, "-1", cp=B, cls="INTERNAL_TRANSFER"),
            B: _row("Ethereum", "0x1", B, "1", cp=A, cls="INTERNAL_TRANSFER")
               + _row("Ethereum", "0x2", B, "3", cp=C, cls="INTERNAL_TRANSFER"),
            C: _row("Ethereum", "0x2", C, "-3", cp=B, cls="INTERNAL_TRANSFER")
               + _row("Ethereum", "0x3", C, "-4", cp=A, cls="INTERNAL_TRANSFER")}
    script = ("import pathlib, sys; p = pathlib.Path('classified.csv'); adds = " + repr(adds)
              + "; p.write_text(p.read_text() + ''.join(adds[a] for a in sys.argv[sys.argv.index('--only') + 1:]))")
    reloaded: list[bool] = []
    s = acc.Syncer(tmp_path, lambda: reloaded.append(True), S)
    s._cli = lambda *a: [sys.executable, "-c", script, *a]  # type: ignore[method-assign]
    s.sync_one(acc.Wallet(A, "Ethereum", "Main", True, False, "evm"))
    for _ in range(200):
        if not s.busy:
            break
        time.sleep(0.05)
    assert s.job is not None and s.job.returncode == 0 and reloaded == [True]
    cmds = [line for line in s.job.log if line.startswith("$ loledger")]
    assert [c.rsplit("--only ", 1)[1] for c in cmds] == [A, B, C]  # A's own tx 0x3 is new but A was already synced
    assert any(line.startswith("Followed transfers through 2 round(s)") for line in s.job.log)


def test_counterparts_include_the_source_of_incoming_transfers(tmp_path: Path) -> None:
    hot = "0x" + "5" * 40
    p = tmp_path / "classified.csv"
    p.write_text(HEAD + _row("Ethereum", "0xw1", A, "1", cp=hot, sub="from_exchange:gemini", linked="gemini:wd:1"))
    before = acc.leg_keys(p)
    p.write_text(p.read_text()
                 + _row("Ethereum", "0xi1", A, "2", cp=B, cls="INTERNAL_TRANSFER")    # received from my wallet B
                 + _row("Ethereum", "0xi2", A, "3", cp=hot, sub="from_eoa"))           # from Gemini's payout wallet
    out = acc.counterparts(p, S, WALLETS, before, [A])
    assert list(out) == [B, "Gemini"] and out[B].startswith("2026-11-20: 2 ETH from Cold")


def test_counterparts_fee_only_stand_in_row_does_not_count_as_having_the_tx(tmp_path: Path) -> None:
    """Syncing wallet A fetched a sweep signed by wallet B: B only got a 0-amount fee row (its own history wasn't fetched)."""
    p = tmp_path / "classified.csv"
    p.write_text(HEAD)
    p.write_text(HEAD + _row("Cardano", "sweep", "stake1hard", "100.5", cp="stake1mid", cls="INTERNAL_TRANSFER",
                             label="Hardware")
                 + _row("Cardano", "sweep", "stake1mid", "0", cp="addr1qhard", cls="OTHER", sub="self_send",
                        label="Ada"))
    wallets = [*WALLETS, acc.Wallet("addr1qhard", "Cardano", "Hardware", True, False, "cardano")]
    assert list(acc.counterparts(p, S, wallets, set(), ["addr1qhard"])) == ["addr1qxyz"]  # Ada = stake1mid


def test_counterparts_awaiting_sync_wallet_and_unknown_sender_api_exchanges(tmp_path: Path) -> None:
    p = tmp_path / "classified.csv"
    p.write_text(HEAD + _row("Cardano", "sweep", "stake1abc", "0", cp="addr1qelse", cls="OTHER", sub="awaiting_sync",
                             label="Ada")
                 + _row("Ethereum", "0xu1", A, "7", cp="0x" + "e" * 40, sub="from_eoa")       # unknown sender
                 + _row("Ethereum", "0xu2", A, "-7", cp="0x" + "e" * 40, sub="to_eoa"))       # sends don't count
    out = acc.counterparts(p, S, WALLETS, set(), ["someone"], closed=["OKX"], api=["Crypto.com", "OKX"])
    assert list(out) == ["addr1qxyz", "Crypto.com"]  # OKX is closed here
    assert "Ada signed sweep" in out["addr1qxyz"] and "maybe a Crypto.com withdrawal" in out["Crypto.com"]


def test_unpaired_exchange_withdrawal_line_names_the_exchange(tmp_path: Path) -> None:
    p = tmp_path / "classified.csv"
    p.write_text(HEAD + _row("MEXC", "mexc:withdrawal:0xabc", "mexc:spot", "-1.85", cp="mexc", sub="withdrawal_to_chain",
                             dest=B))
    [gap] = acc.unpaired(p, S, WALLETS)[B]
    assert gap.text.startswith("2026-11-20: MEXC withdrawal of 1.85 ETH to Cold (Ethereum); that wallet has no")


def test_sync_job_keeps_every_log_line_and_hands_them_out_incrementally() -> None:
    job = acc.SyncJob("t", [])
    job.log.extend(f"line {i}" for i in range(1000))
    st = job.status()
    assert len(st["log"]) == 1000 and st["lines"] == 1000  # nothing dropped
    assert job.status(since=998)["log"] == ["line 998", "line 999"]
    assert job.status(since=5000)["log"] == []


def test_unpaired_withdrawal_goes_to_the_chain_the_exchange_or_token_names(tmp_path: Path) -> None:
    """One address listed on three chains: the line names the one chain the withdrawal went to."""
    d = "0x" + "d" * 40
    ws = [acc.Wallet(d, "Avalanche", "", True, False, "evm"), acc.Wallet(d, "Ethereum", "POAP", True, False, "evm"),
          acc.Wallet(d, "Polygon", "Poly", True, False, "evm"), acc.Wallet(d, "Arbitrum", "Arb", True, False, "evm")]
    p = tmp_path / "classified.csv"

    def gaps(token: str, net: str) -> list[acc.Gap]:
        p.write_text(HEAD.replace("withdrawal_address\n", "withdrawal_address,withdrawal_network\n")
                     + _row("MEXC", "mexc:wd:1", "mexc:spot", "-1.85", cp="mexc", sub="withdrawal_to_chain",
                            dest=d).replace("\n", f",{net}\n").replace(",ETH,", f",{token},"))
        return acc.unpaired(p, S, ws)[d]

    [g] = gaps("MATIC", "")  # no network in the export: MATIC (now POL) is native only on Polygon
    assert "MATIC to Poly (Polygon); that wallet has no matching receipt" in g.text and g.sync
    [g] = gaps("USDT", "USDT-ERC20")  # the exchange names the network
    assert "USDT to POAP (Ethereum)" in g.text
    [g] = gaps("ETH", "")  # ETH is native on Ethereum and Arbitrum: can't tell
    assert "to one of POAP (Ethereum) / Arb (Arbitrum) / Avalanche / Poly (Polygon)" not in g.text
    assert "to one of Avalanche / POAP (Ethereum) / Poly (Polygon) / Arb (Arbitrum): the export doesn't say" in g.text
    [g] = gaps("USDT", "BEP20")  # a chain this address isn't listed on: nothing to sync
    assert "on BEP20, a chain this address isn't listed for" in g.text and not g.sync
    assert acc.counterparts(p, S, ws, set(), []) == {}


def test_network_labels_map_to_chains() -> None:
    for label, chain in [("USDT-ERC20", "Ethereum"), ("BTC-Bitcoin", "Bitcoin"), ("Bitcoin(BTC)", "Bitcoin"),
                         ("CCHAIN", "Avalanche"), ("ethereum", "Ethereum"), ("NIGHT-Cardano", "Cardano"), ("", "")]:
        assert acc.network_chain(label, S) == chain
    assert acc.network_chain("BEP20", S) == "BEP20"  # not a chain we read
