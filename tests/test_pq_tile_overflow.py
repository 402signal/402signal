"""C2SP tile integer bound. Oversized public paths are 404, never OverflowError."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import http as pq_http
from live402.pq import store
from live402.pq import tiles
from tests.pq_test_env import clear_pq_env

MAX = tiles.MAX_TILE_INDEX


class TileOverflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        clear_pq_env()
        self.tmp.cleanup()

    def test_max_accepted(self):
        self.assertEqual(tiles.check_tile_index(MAX), MAX)
        encoded = tiles.encode_tile_index(MAX)
        self.assertEqual(tiles.decode_tile_index(encoded), MAX)
        self.assertEqual(tiles.decode_tile_index(str(MAX)), MAX)

    def test_max_plus_one_rejected(self):
        with self.assertRaises(ValueError):
            tiles.check_tile_index(MAX + 1)
        with self.assertRaises(ValueError):
            tiles.encode_tile_index(MAX + 1)
        with self.assertRaises(ValueError):
            tiles.decode_tile_index(str(MAX + 1))

    def test_huge_decimal_rejected_before_sqlite(self):
        huge = "9" * 40
        with self.assertRaises(ValueError):
            tiles.decode_tile_index(huge)
        status, body, _ctype, _hdrs = pq_http.handle("/pq/log/tile/0/" + huge)
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_huge_grouped_xnnn_path(self):
        grouped = "x999/x999/x999/x999/x999/x999/999"
        with self.assertRaises(ValueError):
            tiles.decode_tile_index(grouped)
        status, body, _ctype, _hdrs = pq_http.handle("/pq/log/tile/0/" + grouped)
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)
        longer = "x999/" * 12 + "999"
        with self.assertRaises(ValueError):
            tiles.decode_tile_index(longer)
        status, body, _ctype, _hdrs = pq_http.handle("/pq/log/tile/entries/" + longer)
        self.assertEqual(status, 404)

    def test_store_rejects_before_sqlite(self):
        self.assertIsNone(store.get_tile(0, MAX + 1))
        self.assertIsNone(store.get_entry_bundle(MAX + 1))
        self.assertIsNone(store.get_tile(0, 10**40))
        self.assertIsNone(store.get_entry_bundle(10**40))
        self.assertIsNone(store.get_tile(0, int("9" * 40)))
        self.assertIsNone(store.get_entry_bundle(int("9" * 40)))

    def test_store_max_binds_without_overflow(self):
        with store._lock:
            conn = store._connect()
            conn.execute(
                "INSERT INTO tiles(level, n, width, data) VALUES (0, ?, 1, ?)",
                (MAX, b"max-tile"),
            )
            conn.execute(
                "INSERT INTO entry_bundles(n, width, data) VALUES (?, 1, ?)",
                (MAX, b"max-bundle"),
            )
            conn.commit()
        self.assertEqual(store.get_tile(0, MAX, 1), b"max-tile")
        self.assertEqual(store.get_entry_bundle(MAX, 1), b"max-bundle")
        self.assertIsNone(store.get_tile(0, MAX + 1, 1))
        self.assertIsNone(store.get_entry_bundle(MAX + 1, 1))

    def test_http_max_is_404_not_traceback(self):
        status, body, _ctype, _hdrs = pq_http.handle("/pq/log/tile/0/" + str(MAX))
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_shared_bound_with_checkpoint(self):
        self.assertEqual(pq_http.MAX_TREE_SIZE, tiles.MAX_TILE_INDEX)
        is_sized, n = pq_http.parse_checkpoint_tree_size("checkpoint/" + str(MAX + 1))
        self.assertTrue(is_sized)
        self.assertIsNone(n)


if __name__ == "__main__":
    unittest.main()
