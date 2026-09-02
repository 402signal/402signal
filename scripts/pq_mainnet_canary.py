#!/usr/bin/env python3
"""Safe MainNet canary operator command.

Default and --summary-only are READ ONLY. They never call signer auth,
never persist AUTHORIZED, and never create a SignedTxn.

  LIVE402_FIXTURE=1 PYTHONPATH=. python3 scripts/pq_mainnet_canary.py
  PYTHONPATH=. python3 scripts/pq_mainnet_canary.py --summary-only
  PYTHONPATH=. python3 scripts/pq_mainnet_canary.py --prepare
  PYTHONPATH=. python3 scripts/pq_mainnet_canary.py --go
  PYTHONPATH=. python3 scripts/pq_mainnet_canary.py --discard-authorized

Does not Fly. Does not set secrets. Does not enable MAINNET_BROADCAST
or MAINNET_CANARY. Does not print Falcon SK, mnemonic, Ed25519 seed,
or HMAC token values.

--prepare: currently fail-closed before signer dial because signer responses
are not response-MAC authenticated. After a separately reviewed coordinated
signer-and-router response binding: preflight → signer once → authenticate →
verify → persist. NO POST.

--go: SEND already-persisted authorization only. Never silently creates
a fresh auth.

LIVE --prepare/--go fails closed in fixture mode.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _router_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_ROOT),
            stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii").strip()
    except Exception:
        return ""


def _print_summary(info: dict) -> None:
    keys = (
        "public_address",
        "network",
        "origin",
        "tree_size",
        "root",
        "amount",
        "sender",
        "receiver",
        "fee",
        "fv",
        "lv",
        "expected_txid",
        "send_state",
        "router_sha",
        "signer_sha",
        "confirm_provider",
        "projected_fee",
        "projected_fv",
        "projected_lv",
        "projected_last_round",
    )
    print("=== MainNet canary summary (no secrets) ===")
    for key in keys:
        if key in info:
            print("%s: %s" % (key, info.get(key)))


def _preflight_gates(*, live_action: bool) -> list[tuple[str, bool, str]]:
    from live402 import fixtures
    from live402.pq import ORIGIN_MAINNET, algo_anchor, log_identity, network as netcfg, store
    from live402.pq import monitor

    rows = []
    network = ""
    try:
        network = log_identity.configured_network()
        rows.append(("1 network=mainnet", network == "mainnet", network or "unset"))
    except log_identity.ConfigError as exc:
        rows.append(("1 network=mainnet", False, str(exc)))
    rows.append(
        (
            "2 genesis mainnet-v1.0",
            algo_anchor.MAINNET_GENESIS_ID == "mainnet-v1.0"
            and algo_anchor.MAINNET_GENESIS_HASH == netcfg.MAINNET_GENESIS_HASH,
            algo_anchor.MAINNET_GENESIS_ID,
        )
    )
    addr = algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME)
    rows.append(("3 mainnet falcon address", bool(addr), "set" if addr else "empty"))
    rows.append(
        (
            "4 mainnet signer token",
            algo_anchor.mainnet_signer_configured(),
            "configured" if algo_anchor.mainnet_signer_configured() else "unset",
        )
    )
    note = ""
    try:
        note = store.latest_checkpoint()
        from live402.pq import checkpoint as ckpt

        ckpt.parse_signed_note(note)
        ckpt_ok = True
    except Exception:
        ckpt_ok = False
    rows.append(("5 valid signed checkpoint", ckpt_ok, "signed" if ckpt_ok else "missing"))
    rows.append(("6 semantic verify (after authorize)", True, "pending_authorize"))
    rows.append(("7 fee cap 30000", algo_anchor.MAX_FEE == 30000, str(algo_anchor.MAX_FEE)))
    rows.append(
        (
            "8 allowlisted submit host",
            netcfg.submit_host_allowlisted("mainnet", netcfg.MAINNET.submit_host),
            netcfg.MAINNET.submit_host,
        )
    )
    rows.append(
        (
            "9 MAINNET_BROADCAST",
            algo_anchor.mainnet_broadcast_requested(),
            "1" if algo_anchor.mainnet_broadcast_requested() else "unset",
        )
    )
    rows.append(
        (
            "10 MAINNET_CANARY",
            algo_anchor.mainnet_canary_requested(),
            "1" if algo_anchor.mainnet_canary_requested() else "unset",
        )
    )
    fixture = fixtures.fixture_mode()
    if live_action:
        rows.append(("11 not live fixture POST", not fixture, "fixture=%s" % fixture))
    else:
        rows.append(("11 fixture ok for read-only", True, "fixture=%s" % fixture))
    health = monitor.preflight()
    signer = health["signer"]
    signer_ok = bool(signer.get("probed") and signer.get("reachable") and signer.get("protocol"))
    signer_detail = signer.get("error") or "ok"
    if not signer.get("probed"):
        signer_detail = "not_probed"
        signer_ok = False
    rows.append(("signer reachable+protocol", signer_ok, signer_detail))
    confirm = health["confirm_provider"]
    confirm_ok = bool(confirm.get("probed") and confirm.get("reachable"))
    confirm_detail = confirm.get("host") or ""
    if not confirm.get("probed"):
        confirm_detail = "not_probed"
        confirm_ok = False
    rows.append(("confirm provider", confirm_ok, confirm_detail))
    rows.append(("db integrity", bool(health["db_integrity"] and health["db_sqlite"]), "ok" if health["db_integrity"] else "fail"))
    rows.append(("epoch", health["epoch"] == "mainnet-v1", health["epoch"]))
    rows.append(("origin", health["origin"] == ORIGIN_MAINNET, health["origin"]))
    return rows


def _prepare_ready(gates: list[tuple[str, bool, str]]) -> bool:
    required = {
        "1 network=mainnet",
        "2 genesis mainnet-v1.0",
        "3 mainnet falcon address",
        "4 mainnet signer token",
        "5 valid signed checkpoint",
        "7 fee cap 30000",
        "8 allowlisted submit host",
        "11 not live fixture POST",
        "signer reachable+protocol",
        "confirm provider",
        "db integrity",
        "epoch",
        "origin",
    }
    for name, ok, _detail in gates:
        if name in required and not ok:
            return False
    return True


def _go_ready(gates: list[tuple[str, bool, str]]) -> bool:
    if not _prepare_ready(gates):
        return False
    flags = {name: ok for name, ok, _detail in gates}
    return bool(flags.get("9 MAINNET_BROADCAST") and flags.get("10 MAINNET_CANARY"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MainNet canary (default refuse, read-only)")
    parser.add_argument(
        "--go",
        action="store_true",
        help="Send already-persisted AUTHORIZED only. Still requires CONFIRM_MAINNET_CANARY=I_UNDERSTAND and both flags.",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Currently fail-closed before signer dial until response authentication exists. Never POSTs.",
    )
    parser.add_argument("--summary-only", action="store_true", help="Read-only summary and exit")
    parser.add_argument(
        "--discard-authorized",
        action="store_true",
        help="Explicitly discard a stale AUTHORIZED blob before SEND_ATTEMPTED. Never after SEND_ATTEMPTED.",
    )
    args = parser.parse_args(argv)

    print("HOLD: no Fly, no secrets, no MainNet txn from this script unless every gate is set.")
    print("READY_FOR_PRODUCTION_KEY_INSTALL remains NO until 402security GO and independent_provider true.")
    print("")
    live_action = bool(args.prepare or args.go)
    print("=== preflight 1-11 ===")
    gates = _preflight_gates(live_action=live_action)
    for name, ok, detail in gates:
        print("[%s] %s (%s)" % ("ok" if ok else "no", name, detail))

    from live402 import fixtures
    from live402.pq import canary, store

    if args.discard_authorized:
        try:
            canary.discard_authorized()
            print("discarded AUTHORIZED (explicit operator action)")
        except canary.CanaryError as exc:
            print("discard: %s" % exc)
            return 1
        if not args.prepare and not args.go:
            return 0

    if live_action and fixtures.fixture_mode():
        print("refused: fixture mode forbids LIVE --prepare/--go")
        return 2

    if args.summary_only or (not args.prepare and not args.go):
        info = canary.inspect(router_sha=_router_sha())["summary"]
        _print_summary(info)
        if args.summary_only:
            return 0
        print("refused: --go not set (default refuse; read-only)")
        return 2

    if args.prepare:
        if not _prepare_ready(gates):
            print("refused: preflight not all affirmative")
            return 2
        try:
            out = canary.prepare(router_sha=_router_sha())
        except canary.CanaryError as exc:
            print("prepare: %s" % exc)
            return 1
        _print_summary(out["summary"])
        print("expected_txid: %s" % out.get("expected_txid"))
        print("state: %s" % out.get("state"))
        print("prepare: AUTHORIZED persisted; no POST")
        if not args.go:
            return 0

    if args.go:
        if not _go_ready(gates):
            print("refused: preflight not all affirmative for send")
            return 2
        if not canary.human_go_set():
            print("refused: CONFIRM_MAINNET_CANARY!=I_UNDERSTAND")
            return 2
        stored = store.last_authorized_checkpoint()
        if not stored or not stored.get("signed"):
            print("refused: no AUTHORIZED blob (use --prepare first; --go does not create auth)")
            return 2
        try:
            out = canary.send_persisted(authorize_human_canary=True, router_sha=_router_sha())
        except canary.CanaryError as exc:
            print("send: %s" % exc)
            return 1
        if out.get("refused"):
            print("refused: %s" % out["refused"])
            return 2
        latest = store.authorized_at(int(stored["tree_size"]))
        print("state: %s" % canary.send_state_of(latest))
        print("result_keys: %s" % sorted(out.get("result", {}).keys()) if isinstance(out.get("result"), dict) else out)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
