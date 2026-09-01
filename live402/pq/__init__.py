"""402Signal PQ1 transparency log (C2SP tiles + RFC 9162 Merkle).

Production log identity is Algorand MainNet epoch mainnet-v1.
Falcon SK is not on this app. Router 6PN client is fail-closed unless
the matching signer token is set. Homepage PQ copy renders only when
last_confirmed has a real independently confirmed txid. GET /transparency
is the first-party read page. Homepage PQ card is injected only when
last_confirmed.size > 0.
"""

from __future__ import annotations

ORIGIN = "402signal.com/pq/log"
ORIGIN_MAINNET = "402signal.com/pq/log/mainnet-v1"
HTTP_PREFIX = "/pq/log"
VOLUME_DB = "/data/pq-log.sqlite"
VOLUME_DB_MAINNET = "/data/pq-log-mainnet.sqlite"
DEFAULT_DB = "/tmp/pq-log.sqlite"
DEFAULT_DB_MAINNET = "/tmp/pq-log-mainnet.sqlite"
EPOCH_TESTNET = "testnet-v1"
EPOCH_MAINNET = "mainnet-v1"
NOTE_FORMAT = "402sg/pq1:b"
NOTE_VERSION = 1
TILE_HEIGHT = 8
TILE_WIDTH = 256
MMD_SECONDS = 15
ANCHOR_SLA_SECONDS = 15 * 60
ANCHOR_SLA_LEAVES = 1000
SPEC_TILES = "C2SP tlog-tiles@v0.1.0"
SPEC_CHECKPOINT = "C2SP tlog-checkpoint@v1.0.0"
SPEC_SIGNED_NOTE = "C2SP signed-note@v1.0.0"
SPEC_MERKLE = "RFC 9162 §2.1 SHA-256"
