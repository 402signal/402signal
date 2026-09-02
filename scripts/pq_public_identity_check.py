#!/usr/bin/env python3
"""Compare public Falcon addresses and Ed25519 vkeys by string and digest.

Never reads a Falcon keyfile. Never accepts an Ed25519 secret.
Never prints a secret. Default Falcon addresses are the already-published
TestNet and MainNet public strings.

  PYTHONPATH=. python3 scripts/pq_public_identity_check.py \\
    --mainnet-vkey-file /secure/vkey.txt \\
    --testnet-vkey-file /offline/testnet.vkey \\
    --compromised-vkey-digest-file /offline/compromised.sha256

Exit 0 when every supplied identity is distinct. Exit 1 on collision.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from live402.pq import log_identity


def _read_text(path: str | None, inline: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return (inline or "").strip()


def _digest(label: str, value: str) -> str:
    digest = log_identity.public_string_digest(value)
    print("%s_digest %s" % (label, digest))
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public-only MainNet identity distinctness")
    parser.add_argument(
        "--mainnet-address",
        default="",
        help="Public MainNet Falcon address (optional; fly.toml default used if empty)",
    )
    parser.add_argument(
        "--testnet-address",
        default=log_identity.TESTNET_FALCON_PUBLIC_ADDRESS,
        help="Public TestNet Falcon address",
    )
    parser.add_argument("--mainnet-vkey", default="", help="Public MainNet C2SP vkey string")
    parser.add_argument("--mainnet-vkey-file", default="", help="File with public MainNet vkey")
    parser.add_argument("--testnet-vkey", default="", help="Public TestNet C2SP vkey string")
    parser.add_argument("--testnet-vkey-file", default="", help="File with public TestNet vkey")
    parser.add_argument(
        "--compromised-vkey",
        default="",
        help="Public compromised MainNet vkey string (not a secret)",
    )
    parser.add_argument("--compromised-vkey-file", default="", help="File with compromised public vkey")
    parser.add_argument(
        "--compromised-vkey-digest",
        default="",
        help="sha256 hex of the compromised public vkey string",
    )
    parser.add_argument(
        "--compromised-vkey-digest-file",
        default="",
        help="File with sha256 hex of the compromised public vkey",
    )
    parser.add_argument(
        "--show-public",
        action="store_true",
        help="Also print full public strings (never secrets)",
    )
    args = parser.parse_args(argv)

    mainnet_addr = (args.mainnet_address or "").strip() or fly_mainnet_address()
    testnet_addr = (args.testnet_address or "").strip()
    errors: list[str] = []

    if mainnet_addr and testnet_addr:
        _digest("testnet_falcon_address", testnet_addr)
        _digest("mainnet_falcon_address", mainnet_addr)
        try:
            log_identity.reject_reused_falcon_address(testnet_addr, mainnet_addr)
            print("MAINNET_FALCON_IDENTITY_DISTINCT ok")
        except log_identity.ConfigError as exc:
            print("MAINNET_FALCON_IDENTITY_DISTINCT fail")
            errors.append(str(exc))
        if args.show_public:
            print("testnet_falcon_address %s" % testnet_addr)
            print("mainnet_falcon_address %s" % mainnet_addr)
    elif not mainnet_addr:
        print("MAINNET_FALCON_IDENTITY_DISTINCT skip (no mainnet address)")

    mainnet_vkey = _read_text(args.mainnet_vkey_file or None, args.mainnet_vkey)
    testnet_vkey = _read_text(args.testnet_vkey_file or None, args.testnet_vkey)
    compromised_vkey = _read_text(args.compromised_vkey_file or None, args.compromised_vkey)
    compromised_digest = _read_text(
        args.compromised_vkey_digest_file or None,
        args.compromised_vkey_digest,
    )
    if compromised_vkey and not compromised_digest:
        compromised_digest = log_identity.public_string_digest(compromised_vkey)

    if mainnet_vkey:
        _digest("mainnet_vkey", mainnet_vkey)
        if args.show_public:
            print("mainnet_vkey %s" % mainnet_vkey)
    if testnet_vkey:
        _digest("testnet_vkey", testnet_vkey)
        if args.show_public:
            print("testnet_vkey %s" % testnet_vkey)
    if compromised_digest:
        print("compromised_vkey_digest %s" % compromised_digest)

    if mainnet_vkey and testnet_vkey:
        try:
            log_identity.reject_reused_ed25519_vkey(testnet_vkey, mainnet_vkey)
            print("MAINNET_VKEY_NE_TESTNET ok")
        except log_identity.ConfigError as exc:
            print("MAINNET_VKEY_NE_TESTNET fail")
            errors.append(str(exc))

    if mainnet_vkey and compromised_digest:
        try:
            log_identity.reject_compromised_ed25519_vkey(mainnet_vkey, compromised_digest)
            print("MAINNET_VKEY_NE_COMPROMISED ok")
        except log_identity.ConfigError as exc:
            print("MAINNET_VKEY_NE_COMPROMISED fail")
            errors.append(str(exc))

    if errors:
        print("identity_check fail")
        return 1
    print("identity_check ok")
    return 0


def fly_mainnet_address() -> str:
    """Public address from fly.toml. Never a keyfile."""
    fly = _ROOT / "fly.toml"
    if not fly.is_file():
        return ""
    prefix = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
    for line in fly.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            _, _, rest = stripped.partition("=")
            return rest.strip().strip('"').strip("'")
    return ""


if __name__ == "__main__":
    sys.exit(main())
