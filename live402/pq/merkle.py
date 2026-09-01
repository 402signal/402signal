"""RFC 9162 §2.1 / RFC 6962 SHA-256 Merkle tree.

leaf = SHA-256(0x00 || entry)
node = SHA-256(0x01 || left || right)
k = largest power of two < n
Empty tree = SHA-256("")

Domain separation is the event type field, not a custom Merkle string.
Do not use RFC 9162 TransItem/SCT. Do not Falcon the log signature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

HASH_SIZE = 32
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_TREE_HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EMPTY_LEAF_HEX = "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def empty_tree_hash() -> bytes:
    return sha256(b"")


def leaf_hash(entry: bytes) -> bytes:
    if not isinstance(entry, (bytes, bytearray)):
        raise TypeError("entry must be bytes")
    return sha256(LEAF_PREFIX + bytes(entry))


def node_hash(left: bytes, right: bytes) -> bytes:
    if len(left) != HASH_SIZE or len(right) != HASH_SIZE:
        # Trillian-line vectors hash raw "N123"||"N456" as the two children
        # when used as HashChildren inputs. Production nodes are 32-byte hashes.
        left_b = bytes(left)
        right_b = bytes(right)
        return sha256(NODE_PREFIX + left_b + right_b)
    return sha256(NODE_PREFIX + left + right)


def largest_power_of_two_less_than(n: int) -> int:
    if n < 2:
        raise ValueError("n must be >= 2")
    k = 1
    while (k << 1) < n:
        k <<= 1
    return k


def mth(entries: Sequence[bytes]) -> bytes:
    """Merkle Tree Hash of raw entries D[0:n] (RFC 9162 §2.1)."""
    return mth_from_leaf_hashes([leaf_hash(e) for e in entries])


def mth_from_leaf_hashes(leaf_hashes: Sequence[bytes], get_range: Callable[[int, int], bytes] | None = None) -> bytes:
    """MTH when level-0 hashes are already RFC 6962 leaf hashes."""
    n = len(leaf_hashes)
    if n == 0:
        return empty_tree_hash()

    def _mth(start: int, end: int) -> bytes:
        width = end - start
        if get_range is not None:
            cached = get_range(start, end)
            if cached is not None:
                return cached
        if width == 1:
            h = bytes(leaf_hashes[start])
        else:
            k = largest_power_of_two_less_than(width)
            h = node_hash(_mth(start, start + k), _mth(start + k, end))
        return h

    return _mth(0, n)


def inclusion_path(index: int, leaf_hashes: Sequence[bytes]) -> list[bytes]:
    """RFC 6962 PATH(m, D[n]). Sibling hashes from leaf toward root."""
    n = len(leaf_hashes)
    if index < 0 or index >= n:
        raise ValueError("index out of range")

    def _path(m: int, start: int, end: int) -> list[bytes]:
        width = end - start
        if width == 1:
            return []
        k = largest_power_of_two_less_than(width)
        if m < start + k:
            return _path(m, start, start + k) + [mth_from_leaf_hashes(leaf_hashes[start + k : end])]
        return _path(m, start + k, end) + [mth_from_leaf_hashes(leaf_hashes[start : start + k])]

    return _path(index, 0, n)


def verify_inclusion(index: int, leaf_hash_b: bytes, path: Sequence[bytes], root: bytes, tree_size: int) -> bool:
    """Recompute root from leaf + RFC 6962 PATH. Fail closed."""
    try:
        if tree_size < 1 or index < 0 or index >= tree_size:
            return False
        if len(leaf_hash_b) != HASH_SIZE or len(root) != HASH_SIZE:
            return False
        computed = root_from_inclusion(index, tree_size, leaf_hash_b, list(path))
        return computed == bytes(root)
    except (TypeError, ValueError, IndexError):
        return False


def root_from_inclusion(index: int, tree_size: int, leaf_hash_b: bytes, path: Sequence[bytes]) -> bytes:
    """Fold PATH(m, D[n]) using the same k-split as inclusion_path."""
    if tree_size < 1 or index < 0 or index >= tree_size:
        raise ValueError("index out of range")

    def _fold(m: int, n: int, leftover: list[bytes]) -> bytes:
        if n == 1:
            if leftover:
                raise ValueError("path too long")
            return bytes(leaf_hash_b)
        k = largest_power_of_two_less_than(n)
        if not leftover:
            raise ValueError("path too short")
        sib = leftover.pop()
        if len(sib) != HASH_SIZE:
            raise ValueError("corrupt sibling")
        if m < k:
            left = _fold(m, k, leftover)
            return node_hash(left, sib)
        right = _fold(m - k, n - k, leftover)
        return node_hash(sib, right)

    leftover = list(path)
    return _fold(index, tree_size, leftover)


def consistency_path(old_size: int, leaf_hashes: Sequence[bytes]) -> list[bytes]:
    """RFC 6962 PROOF(m, D[n]) for 0 < m < n. Empty when old_size == n."""
    n = len(leaf_hashes)
    if old_size < 0 or old_size > n:
        raise ValueError("old_size out of range")
    if old_size == 0 or old_size == n:
        return []
    if old_size > n or n < 1:
        raise ValueError("inconsistent sizes")

    def subproof(m: int, start: int, end: int, first: bool) -> list[bytes]:
        width = end - start
        if m == width:
            if first:
                return []
            return [mth_from_leaf_hashes(leaf_hashes[start:end])]
        k = largest_power_of_two_less_than(width)
        if m <= k:
            return subproof(m, start, start + k, first) + [mth_from_leaf_hashes(leaf_hashes[start + k : end])]
        return subproof(m - k, start + k, end, False) + [mth_from_leaf_hashes(leaf_hashes[start : start + k])]

    return subproof(old_size, 0, n, True)


def mth_range(
    start: int,
    end: int,
    get_range: Callable[[int, int], bytes | None],
    store_range: Callable[[int, int, bytes], None] | None = None,
    leaf_at: Callable[[int], bytes] | None = None,
) -> bytes:
    """RFC 9162 MTH of D[start:end] using cached complete ranges.

    Historical roots stay identical to a full rebuild. Missing cache
    entries are filled from children (O(log n) when the frontier is warm).
    """
    if end < start:
        raise ValueError("invalid range")
    if end == start:
        return empty_tree_hash()
    cached = get_range(start, end)
    if cached is not None:
        return bytes(cached)
    width = end - start
    if width == 1:
        if leaf_at is None:
            raise ValueError("missing leaf hash")
        h = bytes(leaf_at(start))
    else:
        k = largest_power_of_two_less_than(width)
        h = node_hash(
            mth_range(start, start + k, get_range, store_range, leaf_at),
            mth_range(start + k, end, get_range, store_range, leaf_at),
        )
    if store_range is not None:
        store_range(start, end, h)
    return h


def incremental_root(
    new_size: int,
    new_leaf_hash: bytes,
    get_range: Callable[[int, int], bytes | None],
    store_range: Callable[[int, int, bytes], None],
) -> bytes:
    """Append one leaf into an incremental RFC 9162 frontier. O(log n)."""
    if new_size < 1:
        raise ValueError("new_size must be >= 1")
    if len(new_leaf_hash) != HASH_SIZE:
        raise ValueError("leaf hash must be 32 bytes")
    idx = new_size - 1
    store_range(idx, idx + 1, bytes(new_leaf_hash))
    return mth_range(0, new_size, get_range, store_range)


def inclusion_path_cached(
    index: int,
    tree_size: int,
    get_range: Callable[[int, int], bytes | None],
    leaf_at: Callable[[int], bytes] | None = None,
    store_range: Callable[[int, int, bytes], None] | None = None,
) -> list[bytes]:
    """RFC 6962 PATH using cached subtree hashes. Same nodes as inclusion_path."""
    if index < 0 or index >= tree_size:
        raise ValueError("index out of range")

    def _path(m: int, start: int, end: int) -> list[bytes]:
        width = end - start
        if width == 1:
            return []
        k = largest_power_of_two_less_than(width)
        if m < start + k:
            sib = mth_range(start + k, end, get_range, store_range, leaf_at)
            return _path(m, start, start + k) + [sib]
        sib = mth_range(start, start + k, get_range, store_range, leaf_at)
        return _path(m, start + k, end) + [sib]

    return _path(index, 0, tree_size)


def consistency_path_cached(
    old_size: int,
    new_size: int,
    get_range: Callable[[int, int], bytes | None],
    leaf_at: Callable[[int], bytes] | None = None,
    store_range: Callable[[int, int, bytes], None] | None = None,
) -> list[bytes]:
    """RFC 6962 PROOF(m, D[n]) using cached ranges. Empty when old_size == new_size."""
    if old_size < 0 or old_size > new_size:
        raise ValueError("old_size out of range")
    if old_size == 0 or old_size == new_size:
        return []
    if new_size < 1:
        raise ValueError("inconsistent sizes")

    def subproof(m: int, start: int, end: int, first: bool) -> list[bytes]:
        width = end - start
        if m == width:
            if first:
                return []
            return [mth_range(start, end, get_range, store_range, leaf_at)]
        k = largest_power_of_two_less_than(width)
        if m <= k:
            return subproof(m, start, start + k, first) + [
                mth_range(start + k, end, get_range, store_range, leaf_at)
            ]
        return subproof(m - k, start + k, end, False) + [
            mth_range(start, start + k, get_range, store_range, leaf_at)
        ]

    return subproof(old_size, 0, new_size, True)


def verify_consistency(old_size: int, new_size: int, old_root: bytes, new_root: bytes, path: Sequence[bytes]) -> bool:
    """RFC 6962 consistency. Fail closed on corrupt input."""
    try:
        if old_size < 0 or new_size < 0 or old_size > new_size:
            return False
        if old_size == new_size:
            return old_root == new_root and len(path) == 0
        if old_size == 0:
            return len(path) == 0 and new_size >= 0
        if old_size < 1 or new_size < 1:
            return False
        if len(old_root) != HASH_SIZE or len(new_root) != HASH_SIZE:
            return False
        return _verify_consistency(old_size, new_size, old_root, new_root, path)
    except (TypeError, ValueError, IndexError):
        return False


def _verify_consistency(old_size: int, new_size: int, old_root: bytes, new_root: bytes, path: Sequence[bytes]) -> bool:
    if old_size == new_size:
        return old_root == new_root and not path
    fn = old_size - 1
    sn = new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    if not path:
        return False
    # When old_size is a power of two the first path node is old_root itself.
    if fn > 0:
        fr = bytes(path[0])
        sr = bytes(path[0])
        path = path[1:]
    else:
        fr = old_root
        sr = old_root
    for c in path:
        if sn == 0:
            return False
        if len(c) != HASH_SIZE:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            while not (fn & 1) and fn > 0:
                fn >>= 1
                sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == old_root and sr == new_root and sn == 0
