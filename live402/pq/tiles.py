"""C2SP tlog-tiles@v0.1.0 (PIN tagged spec, not untagged main).

Path is /tile/<L>/<N> (height 8 implicit). NOT sumdb /tile/H/L/N.
Full tiles are 256 hashes = 8192 bytes. Partial tiles: .p/<W> for W in 1..255.
Entry bundles: uint16 BE length + bytes, max 65535 per entry.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from live402.pq import TILE_WIDTH
from live402.pq.merkle import HASH_SIZE, leaf_hash, mth_from_leaf_hashes

MAX_ENTRY_BYTES = 65535
LEVEL_MIN = 0
LEVEL_MAX = 63
# Inclusive C2SP / SQLite INTEGER bound. Shared with http checkpoint sizes.
MAX_TILE_INDEX = 9223372036854775807  # 2^63 - 1
# 7 groups cover 10^21 > 2^63-1. Extra part is always overflow.
MAX_INDEX_PARTS = 7
# "tile/entries/" + 7 * "xNNN/" + ".p/255" is well under this.
MAX_TILE_RELPATH_LEN = 96

_INDEX_LAST = re.compile(r"^(0|[1-9][0-9]*)$")
_INDEX_GROUP = re.compile(r"^x[0-9]{3}$")
_INDEX_PADDED = re.compile(r"^[0-9]{3}$")


def check_tile_index(n: int) -> int:
    """Reject indexes outside 0..2^63-1 before any SQLite bind."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError("tile index must be a non-negative integer")
    if n < 0 or n > MAX_TILE_INDEX:
        raise ValueError("tile index out of range")
    return n


def encode_tile_index(n: int) -> str:
    """Zero-padded 3-digit path elements; all but the last begin with x.

    Example from tlog-tiles@v0.1.0: 1234067 → x001/x234/067.
    N < 1000 is plain decimal with no leading zeroes.
    """
    check_tile_index(n)
    if n < 1000:
        return str(n)
    parts: list[str] = []
    x = n
    while True:
        parts.append("%03d" % (x % 1000))
        x //= 1000
        if x == 0:
            break
    parts.reverse()
    out = []
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            out.append("x" + part)
        else:
            out.append(part)
    return "/".join(out)


def decode_tile_index(path: str) -> int:
    raw = (path or "").strip().strip("/")
    if not raw:
        raise ValueError("empty tile index")
    if len(raw) > MAX_TILE_RELPATH_LEN:
        raise ValueError("tile index path too long")
    parts = raw.split("/")
    if len(parts) > MAX_INDEX_PARTS:
        raise ValueError("tile index path too long")
    if len(parts) == 1:
        if not _INDEX_LAST.match(parts[0]):
            raise ValueError("invalid tile index")
        if len(parts[0]) > 19:
            raise ValueError("tile index out of range")
        return check_tile_index(int(parts[0]))
    n = 0
    for i, part in enumerate(parts):
        if n > MAX_TILE_INDEX // 1000:
            raise ValueError("tile index out of range")
        if i < len(parts) - 1:
            if not _INDEX_GROUP.match(part):
                raise ValueError("invalid tile index group")
            n = n * 1000 + int(part[1:])
        else:
            if not _INDEX_PADDED.match(part):
                raise ValueError("invalid tile index tail")
            n = n * 1000 + int(part)
        if n > MAX_TILE_INDEX:
            raise ValueError("tile index out of range")
    return check_tile_index(n)


def tile_path(level: int, n: int, width: int | None = None) -> str:
    _check_level(level)
    check_tile_index(n)
    if width is None or width == TILE_WIDTH:
        return "tile/%d/%s" % (level, encode_tile_index(n))
    if width < 1 or width > 255:
        raise ValueError("partial width must be 1..255")
    return "tile/%d/%s.p/%d" % (level, encode_tile_index(n), width)


def entries_path(n: int, width: int | None = None) -> str:
    check_tile_index(n)
    if width is None or width == TILE_WIDTH:
        return "tile/entries/%s" % encode_tile_index(n)
    if width < 1 or width > 255:
        raise ValueError("partial width must be 1..255")
    return "tile/entries/%s.p/%d" % (encode_tile_index(n), width)


def parse_tile_relpath(rel: str) -> dict:
    """Parse C2SP path relative to the log prefix (no leading slash).

    Returns {kind, level, n, width} where width is None for a full tile.
    Indexes above 2^63-1 and oversized paths raise ValueError (HTTP 404).
    """
    raw = (rel or "").strip().lstrip("/")
    if len(raw) > MAX_TILE_RELPATH_LEN + 16:
        raise ValueError("tile path too long")
    if raw.startswith("tile/entries/"):
        rest = raw[len("tile/entries/") :]
        n, width = _split_index_width(rest)
        return {"kind": "entries", "level": None, "n": n, "width": width}
    if not raw.startswith("tile/"):
        raise ValueError("not a tile path")
    rest = raw[len("tile/") :]
    slash = rest.find("/")
    if slash < 0:
        raise ValueError("missing tile index")
    level_s, idx = rest[:slash], rest[slash + 1 :]
    if not level_s.isdigit() or (len(level_s) > 1 and level_s.startswith("0")):
        raise ValueError("invalid tile level")
    level = int(level_s)
    _check_level(level)
    n, width = _split_index_width(idx)
    return {"kind": "tile", "level": level, "n": n, "width": width}


def _split_index_width(rest: str) -> tuple[int, int | None]:
    if ".p/" in rest:
        idx, w = rest.split(".p/", 1)
        if not w.isdigit() or (len(w) > 1 and w.startswith("0")):
            raise ValueError("invalid partial width")
        width = int(w)
        if width < 1 or width > 255:
            raise ValueError("invalid partial width")
        return decode_tile_index(idx), width
    return decode_tile_index(rest), None


def _check_level(level: int) -> None:
    if not isinstance(level, int) or level < LEVEL_MIN or level > LEVEL_MAX:
        raise ValueError("tile level must be 0..63")


def partial_width(tree_size: int, level: int) -> int:
    """floor(s / 256**l) mod 256. 0 means no partial tile at this level."""
    if tree_size < 0 or level < 0:
        raise ValueError("invalid size/level")
    span = TILE_WIDTH ** level
    return (tree_size // span) % TILE_WIDTH


def full_tile_count(tree_size: int, level: int) -> int:
    span = TILE_WIDTH ** (level + 1)
    return tree_size // span


def encode_entry_bundle(entries: Sequence[bytes]) -> bytes:
    """uint16 BE length-prefixed entries. Max 65535 bytes each."""
    out = bytearray()
    for entry in entries:
        raw = bytes(entry)
        if len(raw) > MAX_ENTRY_BYTES:
            raise ValueError("entry exceeds 65535 bytes")
        out.extend(len(raw).to_bytes(2, "big"))
        out.extend(raw)
    return bytes(out)


def decode_entry_bundle(data: bytes, expected: int | None = None) -> list[bytes]:
    raw = bytes(data or b"")
    i = 0
    out: list[bytes] = []
    while i < len(raw):
        if i + 2 > len(raw):
            raise ValueError("truncated entry length")
        n = int.from_bytes(raw[i : i + 2], "big")
        i += 2
        if i + n > len(raw):
            raise ValueError("truncated entry body")
        out.append(raw[i : i + n])
        i += n
    if expected is not None and len(out) != expected:
        raise ValueError("entry bundle width mismatch")
    return out


def encode_hash_tile(hashes: Sequence[bytes]) -> bytes:
    if not hashes or len(hashes) > TILE_WIDTH:
        raise ValueError("tile must contain 1..256 hashes")
    out = bytearray()
    for h in hashes:
        hb = bytes(h)
        if len(hb) != HASH_SIZE:
            raise ValueError("tile hash must be 32 bytes")
        out.extend(hb)
    return bytes(out)


def decode_hash_tile(data: bytes) -> list[bytes]:
    raw = bytes(data or b"")
    if len(raw) == 0 or len(raw) % HASH_SIZE != 0:
        raise ValueError("tile length is not a multiple of 32")
    n = len(raw) // HASH_SIZE
    if n > TILE_WIDTH:
        raise ValueError("tile wider than 256")
    return [raw[i : i + HASH_SIZE] for i in range(0, len(raw), HASH_SIZE)]


def level0_hashes(entries: Sequence[bytes]) -> list[bytes]:
    return [leaf_hash(e) for e in entries]


def subtree_hash(leaf_hashes: Sequence[bytes], start: int, end: int) -> bytes:
    if start < 0 or end < start or end > len(leaf_hashes):
        raise ValueError("invalid subtree range")
    return mth_from_leaf_hashes(leaf_hashes[start:end])


def tile_hashes_for_level(leaf_hashes: Sequence[bytes], level: int, n: int, width: int) -> list[bytes]:
    """Hashes of tile n at level l, width W (1..256)."""
    if width < 1 or width > TILE_WIDTH:
        raise ValueError("invalid tile width")
    span = TILE_WIDTH ** level
    start0 = n * TILE_WIDTH * span
    out: list[bytes] = []
    for i in range(width):
        lo = start0 + i * span
        hi = lo + span
        if hi > len(leaf_hashes):
            raise ValueError("tile exceeds tree")
        out.append(mth_from_leaf_hashes(leaf_hashes[lo:hi]))
    return out


def tiles_required(tree_size: int) -> list[tuple[int, int, int]]:
    """(level, n, width) tiles that must exist before checkpointing `tree_size`."""
    if tree_size < 0:
        raise ValueError("negative tree size")
    needed: list[tuple[int, int, int]] = []
    if tree_size == 0:
        return needed
    level = 0
    span = 1
    while span <= tree_size:
        full = tree_size // (span * TILE_WIDTH)
        part = (tree_size // span) % TILE_WIDTH
        for n in range(full):
            needed.append((level, n, TILE_WIDTH))
        if part:
            needed.append((level, full, part))
        if span > tree_size:
            break
        level += 1
        if span > tree_size // TILE_WIDTH:
            break
        span *= TILE_WIDTH
        if level > LEVEL_MAX:
            break
    return needed


def bundles_required(tree_size: int) -> list[tuple[int, int]]:
    """(n, width) entry bundles that must exist before checkpointing `tree_size`."""
    if tree_size <= 0:
        return []
    full = tree_size // TILE_WIDTH
    part = tree_size % TILE_WIDTH
    out = [(n, TILE_WIDTH) for n in range(full)]
    if part:
        out.append((full, part))
    return out
