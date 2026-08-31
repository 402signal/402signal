"""In-process isolated Falcon signer. Unsigned txn in, pqsig out via callback.

Library code only on the single app process. Does not serve HTTP /route.
Does not load LIVE402_PQ_LOG_SK. May load LIVE402_PQ_FALCON_SK.
Never log, print, or commit that secret. 402dev never holds it.

Optional stdin helper: one JSON object per line (unsigned txn; bytes as
hex). Stdout: {"pqsig": "<hex>"} or {"error": "sign_failed"}.
"""

from __future__ import annotations

import json
import sys

from live402.pq import algo_anchor


class SignerProcessError(RuntimeError):
    pass


def _bytes_to_hex(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def encode_unsigned_message(txn: dict) -> str:
    """JSON line for the isolated callback. Never includes a private key."""
    if not isinstance(txn, dict):
        raise SignerProcessError("txn required")
    payload = {}
    for key, val in txn.items():
        payload[key] = _bytes_to_hex(val)
    return json.dumps({"txn": payload}, separators=(",", ":"))


def decode_unsigned_message(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise SignerProcessError("empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SignerProcessError("invalid json") from exc
    txn = data.get("txn") if isinstance(data, dict) else None
    if not isinstance(txn, dict):
        raise SignerProcessError("txn required")
    out = dict(txn)
    for key in ("note", "gh", "snd", "rcv", "grp"):
        val = out.get(key)
        if isinstance(val, str):
            try:
                out[key] = bytes.fromhex(val)
            except ValueError as exc:
                raise SignerProcessError("invalid hex") from exc
    return out


def _fail_closed_callback(_txn):
    raise SignerProcessError("callback required")


def handle_request(txn: dict, signer_callback=None) -> bytes:
    """unsigned txn in, pqsig out. Fail closed if we cannot sign."""
    if not isinstance(txn, dict):
        raise SignerProcessError("txn required")
    cb = signer_callback if callable(signer_callback) else _fail_closed_callback
    return algo_anchor.isolated_sign(txn, cb, pk=algo_anchor.current_falcon_sk())


def run_loop(infp, outfp, signer_callback=None) -> None:
    """Read unsigned txns from infp. Write pqsig lines. Never write the SK."""
    for line in infp:
        text = line.strip() if isinstance(line, str) else line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            txn = decode_unsigned_message(text)
            pqsig = handle_request(txn, signer_callback=signer_callback)
            outfp.write(json.dumps({"pqsig": pqsig.hex()}) + "\n")
        except Exception:
            outfp.write(json.dumps({"error": "sign_failed"}) + "\n")
        outfp.flush()


def boot() -> bool:
    """Load Falcon SK only. Never load the Ed25519 log key."""
    return algo_anchor.load_falcon_sk_from_env()


def main(argv: list[str] | None = None) -> None:
    """Stdin helper. No HTTP server. No log-SK load."""
    del argv
    boot()
    run_loop(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
