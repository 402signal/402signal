"""402Signal PQ1 transparency log (C2SP tiles + RFC 9162 Merkle).

Product-GO for the log in CI/local. Not MainNet GO.
Falcon submit is TestNet-gated on the existing app only (no extra Fly
machine). 402security must approve before LIVE402_PQ_FALCON_BROADCAST=1
or any Falcon SK is set, and before homepage PQ copy.
"""

from __future__ import annotations

ORIGIN = "402signal.com/pq/log"
HTTP_PREFIX = "/pq/log"
VOLUME_DB = "/data/pq-log.sqlite"
DEFAULT_DB = "/tmp/pq-log.sqlite"
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
