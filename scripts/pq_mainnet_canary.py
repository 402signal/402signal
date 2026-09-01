#!/usr/bin/env python3
"""Safe MainNet canary operator command.

Default refuses to send. Does not Fly. Does not set secrets. Does not
enable MAINNET_BROADCAST or MAINNET_CANARY. Does not print Falcon SK,
mnemonic, Ed25519 seed, or HMAC token values.

  LIVE402_FIXTURE=1 PYTHONPATH=. python3 scripts/pq_mainnet_canary.py

Authorize + persist + summary always (when identity is present).
Send requires ALL of:
  LIVE402_PQ_FALCON_MAINNET_BROADCAST=1
  LIVE402_PQ_FALCON_MAINNET_CANARY=1
  CONFIRM_MAINNET_CANARY=I_UNDERSTAND
  --go (explicit)
  not fixture-mode live POST
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
    )
    print("=== MainNet canary summary (no secrets) ===")
    for key in keys:
        print("%s: %s" % (key, info.get(key)))


def _preflight_gates() -> list[tuple[str, bool, str]]:
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
    from live402 import fixtures

    rows.append(("11 not live fixture POST", True, "fixture=%s" % fixtures.fixture_mode()))
    health = monitor.preflight()
    rows.append(
        (
            "signer reachable+protocol",
            bool(health["signer"]["available"]),
            health["signer"].get("error") or "ok" if health["signer"]["probed"] else "not_probed",
        )
    )
    rows.append(
        (
            "confirm provider",
            bool(health["confirm_provider"].get("reachable") or not health["confirm_provider"].get("probed")),
            health["confirm_provider"].get("host") or "",
        )
    )
    rows.append(("db integrity", bool(health["db_integrity"] and health["db_sqlite"]), "ok" if health["db_integrity"] else "fail"))
    rows.append(("epoch", health["epoch"] == "mainnet-v1", health["epoch"]))
    rows.append(("origin", health["origin"] == ORIGIN_MAINNET, health["origin"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MainNet canary (default refuse)")
    parser.add_argument(
        "--go",
        action="store_true",
        help="Request send. Still requires CONFIRM_MAINNET_CANARY=I_UNDERSTAND and both flags.",
    )
    parser.add_argument("--summary-only", action="store_true", help="Print summary and exit")
    args = parser.parse_args(argv)

    print("HOLD: no Fly, no secrets, no MainNet txn from this script unless every gate is set.")
    print("READY_FOR_PRODUCTION_KEY_INSTALL remains NO until 402security GO and independent_provider true.")
    print("")
    print("=== preflight 1-11 ===")
    gates = _preflight_gates()
    for name, ok, detail in gates:
        print("[%s] %s (%s)" % ("ok" if ok else "no", name, detail))

    from live402.pq import canary

    try:
        stored = canary.authorize()
    except canary.CanaryError as exc:
        print("authorize: %s" % exc)
        stored = None
    info = canary.summary(stored, router_sha=_router_sha())
    _print_summary(info)
    if args.summary_only:
        return 0
    if not args.go:
        print("refused: --go not set (default refuse)")
        return 2
    if not canary.human_go_set():
        print("refused: CONFIRM_MAINNET_CANARY!=I_UNDERSTAND")
        return 2
    from live402.pq import algo_anchor

    if not algo_anchor.mainnet_broadcast_requested() or not algo_anchor.mainnet_canary_requested():
        print("refused: dual MainNet flags off")
        return 2
    if stored is None:
        print("refused: no AUTHORIZED blob")
        return 2
    try:
        out = canary.send_durable(stored, authorize_human_canary=True)
    except canary.CanaryError as exc:
        print("send: %s" % exc)
        return 1
    from live402.pq import store

    print("state: %s" % canary.send_state_of(store.authorized_at(int(stored["tree_size"]))))
    print("result_keys: %s" % sorted(out.keys()) if isinstance(out, dict) else out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
