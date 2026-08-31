"""Isolated Falcon signer process. Unsigned txn in, pqsig out.

This is the Fly falcon process only. It does not serve HTTP and must
never handle /route. It must not load LIVE402_PQ_LOG_SK (Ed25519 log
key). It may load LIVE402_PQ_FALCON_SK. Never log, print, or commit
that secret. 402dev never holds it.

IPC: one JSON object (unsigned txn; bytes as hex) in, {"pqsig":"<hex>"}
or {"error":"sign_failed"} out. Internal bind only — not port 8080, not
the public HTTP service. Peer is falcon.process.402signal.internal on
Fly 6PN. Fail closed if the signer is down. Never return or echo the SK.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from live402.pq import algo_anchor

PROCESS_GROUP_ENV = "FLY_PROCESS_GROUP"
PROCESS_NAME = "falcon"
APP_PROCESS_NAME = "app"

# Public config only. Not a secret. Never PORT/8080.
DEFAULT_IPC_PORT = 9091
DEFAULT_IPC_PEER = "falcon.process.402signal.internal"
IPC_HOST_ENV = "LIVE402_PQ_SIGNER_HOST"
IPC_PORT_ENV = "LIVE402_PQ_SIGNER_PORT"
IPC_BIND_ENV = "LIVE402_PQ_SIGNER_BIND"
IPC_TIMEOUT = 2.0
_MAX_LINE = 65536
_FORBIDDEN_PORTS = frozenset({8080})


class SignerProcessError(RuntimeError):
    pass


def _bytes_to_hex(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def encode_unsigned_message(txn: dict) -> str:
    """JSON line for the isolated process. Never includes a private key."""
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
    """unsigned txn in, pqsig out. Fail closed if we cannot sign.

    Only a rebuilt PQ1 construction PaymentTxn is signed. Extra keys
    (close, rekey, lx, grp, or unknown) raise before isolated_sign.
    """
    if not isinstance(txn, dict):
        raise SignerProcessError("txn required")
    try:
        rebuilt = algo_anchor.canonical_unsigned_anchor(txn)
    except Exception as exc:
        raise SignerProcessError("sign_failed") from exc
    cb = signer_callback if callable(signer_callback) else _fail_closed_callback
    return algo_anchor.isolated_sign(rebuilt, cb, pk=algo_anchor.current_falcon_sk())


def _response_line(pqsig: bytes | None = None, *, error: bool = False) -> str:
    if error or not isinstance(pqsig, (bytes, bytearray)):
        return json.dumps({"error": "sign_failed"})
    return json.dumps({"pqsig": bytes(pqsig).hex()})


def run_loop(infp, outfp, signer_callback=None) -> None:
    """Read unsigned txns from infp. Write pqsig lines. Never write the SK."""
    for line in infp:
        text = line.strip() if isinstance(line, str) else line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            txn = decode_unsigned_message(text)
            pqsig = handle_request(txn, signer_callback=signer_callback)
            outfp.write(_response_line(pqsig) + "\n")
        except Exception:
            outfp.write(_response_line(error=True) + "\n")
        outfp.flush()


def ipc_port() -> int:
    """Internal signer port. Never PORT/8080."""
    raw = (os.environ.get(IPC_PORT_ENV) or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError as exc:
            raise SignerProcessError("invalid port") from exc
    else:
        port = DEFAULT_IPC_PORT
    if port in _FORBIDDEN_PORTS:
        raise SignerProcessError("signer must not bind 8080")
    return port


def ipc_bind_host() -> str:
    """Bind host. 6PN on Fly; loopback in tests. Never a public HTTP service."""
    explicit = (os.environ.get(IPC_BIND_ENV) or "").strip()
    if explicit:
        return explicit
    if (os.environ.get("FLY_APP_NAME") or "").strip():
        return "fly-local-6pn"
    return "127.0.0.1"


def ipc_peer_host() -> str:
    """HTTP-app peer. Fly process-group DNS by default."""
    explicit = (os.environ.get(IPC_HOST_ENV) or "").strip()
    if explicit:
        return explicit
    return DEFAULT_IPC_PEER


def _recv_line(sock: socket.socket, timeout: float) -> str:
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf:
        if len(buf) >= _MAX_LINE:
            raise SignerProcessError("line too long")
        chunk = sock.recv(min(4096, _MAX_LINE - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    if not buf:
        raise SignerProcessError("empty")
    return buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def handle_connection(conn: socket.socket, signer_callback=None, timeout: float = IPC_TIMEOUT) -> None:
    """One unsigned txn in, one pqsig line out. Never write the SK."""
    try:
        text = _recv_line(conn, timeout)
        txn = decode_unsigned_message(text)
        pqsig = handle_request(txn, signer_callback=signer_callback)
        reply = _response_line(pqsig)
    except Exception:
        reply = _response_line(error=True)
    try:
        conn.sendall((reply + "\n").encode("utf-8"))
    except Exception:
        pass


def serve_ipc(sock: socket.socket, signer_callback=None, stopper=None) -> None:
    """Accept loop. Not HTTP. Not /route. Stop when stopper() is true."""
    sock.settimeout(0.3)
    while True:
        if callable(stopper) and stopper():
            return
        try:
            conn, _addr = sock.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        try:
            handle_connection(conn, signer_callback=signer_callback)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def bind_ipc(host: str | None = None, port: int | None = None) -> socket.socket:
    """Listen on the internal signer port. Refuse 8080."""
    bind_host = (host or ipc_bind_host()).strip()
    bind_port = DEFAULT_IPC_PORT if port is None else int(port)
    if bind_port in _FORBIDDEN_PORTS:
        raise SignerProcessError("signer must not bind 8080")
    infos = socket.getaddrinfo(bind_host, bind_port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    last_err = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(8)
            return sock
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except Exception:
                pass
    raise SignerProcessError("bind failed") from last_err


def request_pqsig(txn: dict, host: str | None = None, port: int | None = None, timeout: float = IPC_TIMEOUT) -> bytes:
    """HTTP-app client: unsigned txn in, pqsig out. Fail closed if down.

    Never returns the SK. Connection errors raise SignerProcessError.
    """
    if not isinstance(txn, dict):
        raise SignerProcessError("txn required")
    dest_host = (host or ipc_peer_host()).strip()
    dest_port = ipc_port() if port is None else int(port)
    if dest_port in _FORBIDDEN_PORTS:
        raise SignerProcessError("signer must not use 8080")
    line = encode_unsigned_message(txn) + "\n"
    try:
        with socket.create_connection((dest_host, dest_port), timeout=timeout) as sock:
            sock.sendall(line.encode("utf-8"))
            raw = _recv_line(sock, timeout)
    except SignerProcessError:
        raise
    except Exception as exc:
        raise SignerProcessError("unavailable") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SignerProcessError("invalid json") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise SignerProcessError("sign_failed")
    hex_sig = data.get("pqsig")
    if not isinstance(hex_sig, str) or not hex_sig:
        raise SignerProcessError("sign_failed")
    try:
        return bytes.fromhex(hex_sig)
    except ValueError as exc:
        raise SignerProcessError("sign_failed") from exc


def boot() -> bool:
    """Load Falcon SK only. Never load the Ed25519 log key."""
    group = (os.environ.get(PROCESS_GROUP_ENV) or "").strip()
    if group == APP_PROCESS_NAME:
        raise SignerProcessError("isolated signer must not run in the app process")
    return algo_anchor.load_falcon_sk_from_env()


def main(argv: list[str] | None = None) -> None:
    """Process entrypoint. Internal IPC only. No HTTP server. No log-SK load."""
    del argv
    boot()
    sock = bind_ipc()
    try:
        serve_ipc(sock)
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
