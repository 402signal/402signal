#!/usr/bin/env python3
"""Derive a C2SP Ed25519 vkey from a secret on stdin or --sk-file.

Never accepts the secret as a CLI argument. Never prints the secret.
Prints only the public vkey and/or its sha256 digest.

  umask 077
  PYTHONPATH=. python3 scripts/pq_derive_vkey.py \\
    --origin 402signal.com/pq/log/mainnet-v1 \\
    --sk-file /secure/sk.hex \\
    --vkey-out /secure/vkey.txt \\
    --digest-out /secure/vkey.sha256

Do not generate production keys in CI. Tests use ephemeral in-memory seeds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_ORIGIN = "402signal.com/pq/log/mainnet-v1"


def _looks_like_hex_secret(text: str) -> bool:
    raw = (text or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    raw = "".join(raw.split())
    return len(raw) == 64 and all(ch in "0123456789abcdef" for ch in raw)


def refuse_secret_cli_args(argv: list[str]) -> None:
    """Fail closed if a 32-byte hex seed appears as a CLI token."""
    for arg in argv:
        if arg.startswith("-"):
            continue
        if _looks_like_hex_secret(arg):
            raise SystemExit("refusing secret-shaped CLI argument")


def derive_vkey(raw_sk: str, origin: str) -> str:
    """Return the C2SP vkey. Never logs raw_sk."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from live402.pq import checkpoint as ckpt
    from live402.pq import receipt

    name = (origin or "").strip()
    if not name or "\n" in name or "+" in name:
        raise SystemExit("invalid origin")
    try:
        key = receipt._parse_log_sk(raw_sk)
    except receipt.SignerConfigError:
        raise SystemExit("malformed sk") from None
    pk = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return ckpt.vkey_encode(name, pk)


def _read_sk(sk_file: str | None) -> str:
    if sk_file:
        path = Path(sk_file)
        if not path.is_file():
            raise SystemExit("sk-file missing")
        return path.read_text(encoding="utf-8")
    if sys.stdin.isatty():
        raise SystemExit("refusing tty stdin; use --sk-file or a pipe")
    return sys.stdin.read()


def _write_public(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        refuse_secret_cli_args(argv)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Derive public C2SP vkey (never prints SK)")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="C2SP key name / log origin")
    parser.add_argument("--sk-file", help="0600 file holding hex seed or PKCS8 PEM")
    parser.add_argument("--vkey-out", help="Write public vkey to this path")
    parser.add_argument("--digest-out", help="Write sha256(vkey) hex to this path")
    parser.add_argument("--digest-only", action="store_true", help="Print digest, not vkey")
    args = parser.parse_args(argv)

    try:
        raw = _read_sk(args.sk_file)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        vkey = derive_vkey(raw, args.origin)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        raw = ""

    from live402.pq import log_identity

    digest = log_identity.public_string_digest(vkey)
    if args.vkey_out:
        _write_public(Path(args.vkey_out), vkey + "\n")
    if args.digest_out:
        _write_public(Path(args.digest_out), digest + "\n")
    if args.digest_only:
        print(digest)
    else:
        print(vkey)
    return 0


if __name__ == "__main__":
    sys.exit(main())
