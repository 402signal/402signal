"""RFC 9162 §2.1 / C2SP tiles / SQLite store. No keys. No network."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import ORIGIN, VOLUME_DB, merkle, store, tiles, trust
from live402.pq.merkle import EMPTY_LEAF_HEX, EMPTY_TREE_HEX


def _h(hex_s: str) -> bytes:
    return bytes.fromhex(hex_s)


class RFC6962VectorTests(unittest.TestCase):
    def test_empty_tree_is_sha256_empty(self):
        self.assertEqual(merkle.empty_tree_hash().hex(), EMPTY_TREE_HEX)
        self.assertEqual(EMPTY_TREE_HEX, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(merkle.mth([]).hex(), EMPTY_TREE_HEX)
        self.assertEqual(merkle.mth_from_leaf_hashes([]).hex(), EMPTY_TREE_HEX)

    def test_empty_leaf_trillian(self):
        self.assertEqual(merkle.leaf_hash(b"").hex(), EMPTY_LEAF_HEX)
        self.assertEqual(EMPTY_LEAF_HEX, "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d")

    def test_leaf_L123456_trillian(self):
        got = merkle.leaf_hash(b"L123456").hex()
        want = hashlib.sha256(b"\x00" + b"L123456").hexdigest()
        self.assertEqual(got, want)
        self.assertEqual(got, "395aa064aa4c29f7010acfe3f25db9485bbd4b91897b6ad7ad547639252b4d56")

    def test_node_N123_N456_trillian(self):
        got = merkle.node_hash(b"N123", b"N456").hex()
        want = hashlib.sha256(b"\x01" + b"N123" + b"N456").hexdigest()
        self.assertEqual(got, want)
        self.assertEqual(got, "aa217fe888e47007fa15edab33c2b492a722cb106c64667fc2b044444de66bbb")

    def test_k_is_largest_power_of_two_strictly_less_than_n(self):
        self.assertEqual(merkle.largest_power_of_two_less_than(2), 1)
        self.assertEqual(merkle.largest_power_of_two_less_than(3), 2)
        self.assertEqual(merkle.largest_power_of_two_less_than(5), 4)
        self.assertEqual(merkle.largest_power_of_two_less_than(8), 4)
        self.assertEqual(merkle.largest_power_of_two_less_than(9), 8)


class InclusionConsistencyTests(unittest.TestCase):
    def _leaves(self, n: int) -> list[bytes]:
        return [b"leaf-%d" % i for i in range(n)]

    def test_inclusion_single_leaf_empty_path(self):
        hashes = [merkle.leaf_hash(b"only")]
        path = merkle.inclusion_path(0, hashes)
        self.assertEqual(path, [])
        root = merkle.mth_from_leaf_hashes(hashes)
        self.assertTrue(merkle.verify_inclusion(0, hashes[0], path, root, 1))

    def test_inclusion_and_consistency_many_sizes(self):
        raw = self._leaves(17)
        hashes = [merkle.leaf_hash(e) for e in raw]
        for n in range(1, 18):
            root = merkle.mth_from_leaf_hashes(hashes[:n])
            for i in range(n):
                path = merkle.inclusion_path(i, hashes[:n])
                self.assertTrue(
                    merkle.verify_inclusion(i, hashes[i], path, root, n),
                    "inclusion i=%s n=%s" % (i, n),
                )
                bad = list(path)
                if bad:
                    flipped = bytearray(bad[0])
                    flipped[0] ^= 0xFF
                    bad[0] = bytes(flipped)
                    self.assertFalse(merkle.verify_inclusion(i, hashes[i], bad, root, n))
                self.assertFalse(merkle.verify_inclusion(i, hashes[i], path + [os.urandom(32)], root, n))
            for m in range(1, n + 1):
                old_root = merkle.mth_from_leaf_hashes(hashes[:m])
                proof = merkle.consistency_path(m, hashes[:n])
                self.assertTrue(
                    merkle.verify_consistency(m, n, old_root, root, proof),
                    "consistency m=%s n=%s" % (m, n),
                )
                if proof:
                    flipped = bytearray(proof[0])
                    flipped[0] ^= 0x01
                    corrupt = [bytes(flipped)] + list(proof[1:])
                    self.assertFalse(merkle.verify_consistency(m, n, old_root, root, corrupt))

    def test_corrupt_proof_rejected(self):
        hashes = [merkle.leaf_hash(b"a"), merkle.leaf_hash(b"b"), merkle.leaf_hash(b"c")]
        root = merkle.mth_from_leaf_hashes(hashes)
        path = merkle.inclusion_path(1, hashes)
        self.assertFalse(merkle.verify_inclusion(1, hashes[0], path, root, 3))
        self.assertFalse(merkle.verify_inclusion(1, hashes[1], path, os.urandom(32), 3))
        self.assertFalse(merkle.verify_inclusion(-1, hashes[1], path, root, 3))
        self.assertFalse(merkle.verify_inclusion(1, hashes[1], [b"short"], root, 3))


class TilePathTests(unittest.TestCase):
    def test_index_encoding_from_spec(self):
        self.assertEqual(tiles.encode_tile_index(0), "0")
        self.assertEqual(tiles.encode_tile_index(255), "255")
        self.assertEqual(tiles.encode_tile_index(999), "999")
        self.assertEqual(tiles.encode_tile_index(1234067), "x001/x234/067")
        self.assertEqual(tiles.decode_tile_index("x001/x234/067"), 1234067)
        self.assertEqual(tiles.decode_tile_index("0"), 0)
        self.assertEqual(tiles.encode_tile_index(1000), "x001/000")
        self.assertEqual(tiles.decode_tile_index("x001/000"), 1000)

    def test_path_is_c2sp_not_sumdb(self):
        self.assertEqual(tiles.tile_path(0, 0), "tile/0/0")
        self.assertEqual(tiles.tile_path(0, 0, 12), "tile/0/0.p/12")
        self.assertEqual(tiles.entries_path(0, 3), "tile/entries/0.p/3")
        parsed = tiles.parse_tile_relpath("tile/0/x001/234.p/8")
        self.assertEqual(parsed["kind"], "tile")
        self.assertEqual(parsed["level"], 0)
        self.assertEqual(parsed["n"], 1234)
        self.assertEqual(parsed["width"], 8)
        self.assertNotIn("/tile/8/", tiles.tile_path(0, 1))

    def test_entry_bundle_roundtrip(self):
        blob = tiles.encode_entry_bundle([b"aa", b"bbb"])
        self.assertEqual(blob[:2], (2).to_bytes(2, "big"))
        self.assertEqual(tiles.decode_entry_bundle(blob), [b"aa", b"bbb"])
        with self.assertRaises(ValueError):
            tiles.encode_entry_bundle([b"x" * 65536])


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()

    def tearDown(self):
        store.install_after_durable_hook(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def test_volume_path_is_separate_sqlite(self):
        self.assertEqual(VOLUME_DB, "/data/pq-log.sqlite")
        self.assertNotEqual(VOLUME_DB, "/data/catalog.sqlite")
        self.assertNotEqual(VOLUME_DB, "/data/live402-history.sqlite")

    def test_append_publish_and_duplicate_idempotent(self):
        first = store.append(b"hello")
        self.assertEqual(first["idx"], 0)
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["size"], 1)
        self.assertTrue(store.ready_to_checkpoint(1))
        again = store.append(b"hello")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["idx"], 0)
        self.assertEqual(store.size(), 1)
        second = store.append(b"world")
        self.assertEqual(second["idx"], 1)
        self.assertEqual(store.size(), 2)
        self.assertEqual(store.root(), merkle.mth([b"hello", b"world"]))
        path = store.inclusion_path(0)
        self.assertTrue(merkle.verify_inclusion(0, first["leaf_hash"], path, store.root(), 2))
        tile0 = store.get_tile(0, 0, 2)
        self.assertIsNotNone(tile0)
        self.assertEqual(len(tile0), 64)
        bundle = store.get_entry_bundle(0, 2)
        self.assertEqual(tiles.decode_entry_bundle(bundle), [b"hello", b"world"])

    def test_refuses_checkpoint_when_tiles_missing(self):
        store.append(b"one")
        with store._lock:
            conn = store._connect()
            conn.execute("DELETE FROM tiles")
            conn.execute("DELETE FROM entry_bundles")
            conn.commit()
        self.assertFalse(store.ready_to_checkpoint(1))
        with self.assertRaises(ValueError):
            store.save_checkpoint(1, "not-a-real-note\n")
        store.publish_up_to(1)
        self.assertTrue(store.ready_to_checkpoint(1))
        store.save_checkpoint(1, "placeholder\n")
        self.assertIn("placeholder", store.latest_checkpoint())

    def test_empty_root_before_leaves(self):
        self.assertEqual(store.size(), 0)
        self.assertEqual(store.root().hex(), EMPTY_TREE_HEX)
        self.assertTrue(store.ready_to_checkpoint(0))


class TrustRootTests(unittest.TestCase):
    def test_repo_descriptor_is_fail_closed_sha256_ed25519(self):
        desc = trust.trust_root()
        self.assertEqual(desc["origin"], ORIGIN)
        self.assertEqual(desc["merkle"]["algorithm"], "SHA-256")
        self.assertEqual(desc["log_signature"]["algorithm"], "ed25519")
        self.assertEqual(desc["witness_policy"], [])
        self.assertEqual(desc["rotation"], "new-shard")
        self.assertTrue(desc["not_mainnet_go"])

    def test_unknown_algorithm_fail_closed(self):
        bad = trust.load_descriptor()
        bad = dict(bad)
        bad["merkle"] = {"algorithm": "SHA3-256", "profile": "nope"}
        with self.assertRaises(trust.UnknownAlgorithm):
            trust.validate_descriptor(bad)
        mixed = trust.load_descriptor()
        mixed = dict(mixed)
        mixed["rotation"] = "mixed-hash-tree"
        with self.assertRaises(trust.UnknownAlgorithm):
            trust.validate_descriptor(mixed)


if __name__ == "__main__":
    unittest.main()
