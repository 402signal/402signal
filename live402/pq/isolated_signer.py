"""Router-side 6PN client wrapper. Falcon SK is not on this app.

Use live402.pq.signer_client. This module re-exports the pq-anchor/1
client. It does not bind a signer, load a Falcon SK, or submit to
Algorand. LIVE402_PQ_SIGNER_TOKEN unset: never dial.
"""

from __future__ import annotations

from live402.pq.signer_client import (
    DEFAULT_IPC_HOST,
    DEFAULT_IPC_PORT,
    IPC_HOST_ENV,
    IPC_PORT_ENV,
    REQUEST_KEYS,
    SignerClientError,
    build_request,
    canonical_bytes,
    encode_request_line,
    ipc_peer_host,
    ipc_port,
    mac_hex,
    request_sign,
    signer_token,
    token_configured,
)

# Historical name. Same fail-closed error as the 6PN client.
SignerProcessError = SignerClientError

__all__ = (
    "DEFAULT_IPC_HOST",
    "DEFAULT_IPC_PORT",
    "IPC_HOST_ENV",
    "IPC_PORT_ENV",
    "REQUEST_KEYS",
    "SignerClientError",
    "SignerProcessError",
    "build_request",
    "canonical_bytes",
    "encode_request_line",
    "ipc_peer_host",
    "ipc_port",
    "mac_hex",
    "request_sign",
    "signer_token",
    "token_configured",
)
