"""Remaining A-W hardening: durable states, confirmed invariants, trust, /ready, schemas."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

os.environ.setdefault("LIVE402_FIXTURE", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live402 import catalog, discover, fixtures, hydrate, mcp, payment, schema_fields, server
from live402.pq import ORIGIN, algo_anchor, events, receipt, store, trust, worker
from live402.route import handle_route
from live402.server import Handler


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _txid(ch="B"):
    return (ch * 52)[:52]


class V2ReceiptRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.key = Ed25519PrivateKey.generate()
        self.vkey = receipt.configure_signer(self.key)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LIVE402_PQ_LOG", None)
        self.tmp.cleanup()

    def test_attach_receipt_verify_receipt_roundtrip(self):
        out = receipt.attach_to_route(
            {"live": True, "url": "https://fixture.402signal.local/weather"},
            {"need": "private-need-text", "url": "https://fixture.402signal.local/weather"},
        )
        tr = out["pq_trust"]["transparency"]
        self.assertEqual(tr["status"], "pending")
        self.assertEqual(tr["state"], "checkpoint_signed")
        self.assertEqual(tr["leaf_type"], events.TYPE_ROUTE_DECISION_V3)
        verified = receipt.verify_receipt(tr["receipt"], self.vkey)
        self.assertEqual(verified["body"]["origin"], ORIGIN)
        self.assertTrue(events.verify_reveal_v3(tr["reveal"]["commitment"], tr["reveal"]))
        receipt.verify_route_receipt(tr["receipt"], tr["reveal"], self.vkey)
        leaf = json.loads(store.leaf_at(tr["index"])["body"].decode("utf-8"))
        self.assertEqual(leaf["commitment"], tr["reveal"]["commitment"])
        self.assertEqual(set(leaf), {"type", "ts", "nonce", "commitment"})
        self.assertNotIn("salt", leaf)
        self.assertNotIn("live", leaf)
        self.assertNotIn("private-need-text", json.dumps(leaf))
        self.assertNotIn("anonymous", json.dumps(leaf).lower())

    def test_no_signer_is_logged_not_pending_or_signed(self):
        receipt.configure_signer(None)
        out = receipt.attach_to_route(
            {"live": True, "url": "https://fixture.402signal.local/weather"},
            {"need": "weather"},
        )
        tr = out["pq_trust"]["transparency"]
        self.assertEqual(tr["status"], "logged_uncheckpointed")
        self.assertEqual(tr["state"], "logged_uncheckpointed")
        self.assertNotEqual(tr["status"], "pending")
        self.assertIsNotNone(store.leaf_at(tr["index"]))
        self.assertFalse(store.latest_checkpoint())
        rec = tr.get("receipt") or {}
        self.assertNotIn("checkpoint", rec)

    def test_require_transparency_fails_without_signed_checkpoint(self):
        receipt.configure_signer(None)
        os.environ["LOCAL_FREE"] = "1"
        try:
            code, body, _extra = handle_route(
                {
                    "need": "weather",
                    "url": "https://fixture.402signal.local/weather",
                    "require_transparency": True,
                },
                {},
                "https://402signal.com/route",
            )
            self.assertEqual(code, 503)
            self.assertIn("transparency", (body.get("error") or "").lower())
        finally:
            os.environ.pop("LOCAL_FREE", None)


class ConfirmedInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _save(self, size, origin=ORIGIN, root=None, txid=None, rnd=12, at=1):
        root = root if root is not None else ("ab" * 32)
        return store.save_confirmed_checkpoint(
            tree_size=size,
            origin=origin,
            root=root,
            txid=txid or _txid(),
            confirmed_round=rnd,
            at=at,
        )

    def test_monotonic_origin_root_txid_conflict_and_idempotent(self):
        root = "ab" * 32
        first = self._save(2, root=root, txid=_txid("B"), rnd=10, at=100)
        self.assertEqual(first["size"], 2)
        again = self._save(2, root=root, txid=_txid("B"), rnd=10, at=100)
        self.assertEqual(again["size"], 2)
        with self.assertRaises(store.ConflictError):
            self._save(2, root=root, txid=_txid("C"), rnd=10, at=100)
        with self.assertRaises(store.ConflictError):
            self._save(1, root=root, txid=_txid("D"), rnd=11, at=101)
        with self.assertRaises(store.ConflictError):
            self._save(3, root="zz", txid=_txid("E"), rnd=12, at=102)
        with self.assertRaises(store.ConflictError):
            self._save(3, root=root, txid="not-a-txid", rnd=12, at=102)
        with self.assertRaises(store.ConflictError):
            self._save(3, root=root, txid=_txid("E"), rnd=0, at=102)
        store.save_authorized_checkpoint(
            tree_size=4,
            origin=ORIGIN,
            root=root,
            checkpoint="note",
            request_id="r1",
            signed=b"sig",
            at=200,
        )
        with self.assertRaises(store.ConflictError):
            self._save(4, origin="other.origin", root=root, txid=_txid("F"), rnd=20, at=201)
        ok = self._save(4, origin=ORIGIN, root=root, txid=_txid("F"), rnd=20, at=201)
        self.assertEqual(ok["size"], 4)
        self.assertEqual(ok["txid"], _txid("F"))


class SignedTxnVerifyTests(unittest.TestCase):
    def setUp(self):
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(lambda: os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None))

    def _signed(self, **txn_over):
        from live402 import algo_tx

        addr = payment.DEFAULT_PAYTO_ALGORAND
        root = b"\x11" * 32
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        gh = __import__("base64").b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            addr, addr, 0, 3000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note
        )
        txn.update(txn_over)
        blob = algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": b"pk" + bytes(range(14)), "sch": "f1", "sig": b"sig" + bytes(range(29)), "slt": 0},
                "txn": txn,
            }
        )
        return blob, root

    def test_valid_pay0_self_falcon_then_rejects(self):
        blob, root = self._signed()
        out = algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN,
            expected_size=1,
            expected_root=root,
            expected_address=payment.DEFAULT_PAYTO_ALGORAND,
        )
        self.assertEqual(out["tree_size"], 1)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                b"not-msgpack",
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=payment.DEFAULT_PAYTO_ALGORAND,
            )
        bad_amt, _root = self._signed(amt=1)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                bad_amt,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=payment.DEFAULT_PAYTO_ALGORAND,
            )
        bad_gen, _root = self._signed(gen=algo_anchor.MAINNET_GENESIS_ID)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                bad_gen,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=payment.DEFAULT_PAYTO_ALGORAND,
            )
        rekey, _root = self._signed(rekey=b"\x01" * 32)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                rekey,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=payment.DEFAULT_PAYTO_ALGORAND,
            )


class ReadyAndHeaderTests(unittest.TestCase):
    def test_ready_503_when_local_log_smaller_than_confirmed(self):
        tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(tmp.name, "pq-log.sqlite")
        store.reset()
        httpd, port = _serve()
        try:
            for i in range(9):
                store.append(("leaf-%s" % i).encode())
            self.assertEqual(store.size(), 9)
            store.save_confirmed_checkpoint(
                tree_size=10,
                origin=ORIGIN,
                root=bytes(range(32)),
                txid=_txid("B"),
                confirmed_round=99,
                at=100,
            )
            from live402.pq.transparency import log_integrity_error
            from live402 import ready as ready_mod

            self.assertTrue(
                log_integrity_error(store.size(), store.last_confirmed_checkpoint())
            )
            payload = ready_mod.readiness()
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["checks"]["pq_log"])
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/ready")
            res = conn.getresponse()
            raw = res.read()
            conn.close()
            self.assertEqual(res.status, 503)
            body = json.loads(raw.decode("utf-8"))
            self.assertFalse(body["ok"])
            self.assertFalse(body["checks"]["pq_log"])
            blob = raw.decode("utf-8").lower()
            self.assertNotIn("/data", blob)
            self.assertNotIn("sqlite", blob)
            self.assertNotIn(tmp.name.lower(), blob)
            self.assertNotIn("live402_pq_log_sk", blob)
            self.assertNotIn("live402_pq_falcon_sk", blob)
            self.assertNotIn("private", blob)
            self.assertNotIn("inconsistent", blob)
        finally:
            httpd.shutdown()
            httpd.server_close()
            store.reset()
            os.environ.pop("LIVE402_PQ_LOG_DB", None)
            tmp.cleanup()

    def test_ready_and_server_header(self):
        httpd, port = _serve()
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/ready")
            res = conn.getresponse()
            raw = res.read()
            hdrs = {k.lower(): v for k, v in res.getheaders()}
            conn.close()
            self.assertEqual(res.status, 200)
            body = json.loads(raw.decode("utf-8"))
            self.assertTrue(body["ok"])
            for key in ("storage", "catalog", "history", "pq_log"):
                self.assertTrue(body["checks"][key])
            blob = raw.decode("utf-8").lower()
            self.assertNotIn("/data", blob)
            self.assertNotIn("sqlite", blob)
            self.assertNotIn("live402_pq_log_sk", blob)
            self.assertNotIn("private", blob)
            server_hdr = hdrs.get("server", "")
            self.assertNotIn("python", server_hdr.lower())
            self.assertNotIn("cpython", server_hdr.lower())
            self.assertEqual(server_hdr, "402Signal")

            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/health")
            res = conn.getresponse()
            res.read()
            hdrs = {k.lower(): v for k, v in res.getheaders()}
            conn.close()
            self.assertEqual(hdrs.get("server"), "402Signal")

            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/pq/log/trust")
            res = conn.getresponse()
            raw = res.read()
            conn.close()
            self.assertEqual(res.status, 200)
            desc = json.loads(raw.decode("utf-8"))
            self.assertEqual(desc["falcon"]["network"], "testnet-v1.0")
            self.assertEqual(desc["falcon"]["allowed_broadcast"], "testnet")
            self.assertTrue(desc["not_mainnet_go"])
            self.assertEqual(desc["witness_policy"], [])
            self.assertNotIn("sk", desc.get("log_signature") or {})
            self.assertNotIn("private_key", desc)
        finally:
            httpd.shutdown()
            httpd.server_close()


class SchemaAndTrustSyncTests(unittest.TestCase):
    def test_shared_anyof_and_constraints(self):
        shared = list(schema_fields.NEED_OR_URL_ANYOF)
        self.assertEqual(mcp.INPUT_SCHEMA["anyOf"], shared)
        self.assertEqual(schema_fields.route_body_schema()["anyOf"], shared)
        body = payment.BAZAAR_EXTENSION["schema"]["properties"]["input"]["properties"]["body"]
        self.assertEqual(body["anyOf"], shared)
        self.assertEqual(payment.BAZAAR_MCP["info"]["input"]["inputSchema"]["anyOf"], shared)
        spec = discover.openapi_spec("https://402signal.com")
        route_schema = spec["paths"]["/route"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(route_schema["anyOf"], shared)
        for key in ("accept_payTo_change", "require_transparency", "objective"):
            self.assertIn(key, mcp.INPUT_SCHEMA["properties"])
            self.assertIn(key, route_schema["properties"])
        self.assertEqual(set(mcp.INPUT_SCHEMA["properties"]["objective"]["enum"]), set(schema_fields.OBJECTIVES))

    def test_public_descriptor_is_testnet_runtime_vkey(self):
        desc = trust.public_descriptor()
        self.assertEqual(desc["falcon"]["network"], "testnet-v1.0")
        self.assertNotEqual(desc["falcon"]["network"], "mainnet-v1.0")
        self.assertEqual(desc["witness_policy"], [])
        self.assertEqual(desc["log_signature"]["vkey"], trust.vkey())
        self.assertNotIn("sk", json.dumps(desc))

    def test_v2_public_leaf_docs(self):
        reveals = schema_fields.v2_public_leaf_reveals()
        omits = schema_fields.v2_public_leaf_omits()
        self.assertIn("commitment", reveals)
        self.assertIn("salt", omits)
        self.assertIn("need", omits)
        self.assertNotIn("anonymous", " ".join(reveals))

    def test_v3_public_leaf_docs(self):
        reveals = schema_fields.v3_public_leaf_reveals()
        omits = schema_fields.v3_public_leaf_omits()
        self.assertEqual(set(reveals), {"type", "ts", "nonce", "commitment"})
        self.assertIn("salt", omits)
        self.assertIn("live", omits)
        self.assertIn("payTo", omits)
        self.assertNotIn("anonymous", " ".join(reveals))
        self.assertIn("policy.objective", schema_fields.v3_bound_fields())
        self.assertIn("catalog_claimed", schema_fields.v3_unbound_fields())

    def test_worker_decoupled_and_fixture_network_free(self):
        src = inspect.getsource(catalog._trickle_loop)
        self.assertNotIn("pq_worker", src)
        self.assertNotIn("tick()", src)
        self.assertTrue(fixtures.fixture_mode())
        self.assertFalse(worker.worker_running())
        worker.start_worker()
        self.assertFalse(worker.worker_running())
        main_src = inspect.getsource(server.main)
        self.assertIn("start_worker", main_src)

    def test_seller_schema_warning_constant(self):
        self.assertIn("system prompts", hydrate.CLIENT_SCHEMA_WARNING)
        self.assertIn("untrusted", hydrate.CLIENT_SCHEMA_WARNING.lower())
        self.assertIn("$ref", hydrate.CLIENT_SCHEMA_WARNING)


class BackupScriptTests(unittest.TestCase):
    def test_backup_and_restore_roundtrip(self):
        import importlib.util
        import sqlite3
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]

        def _load(name):
            path = root / "scripts" / ("%s.py" % name)
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        backup_sqlite = _load("backup_sqlite")
        restore_sqlite = _load("restore_sqlite")

        tmp = tempfile.TemporaryDirectory()
        src = Path(tmp.name) / "catalog.sqlite"
        conn = sqlite3.connect(src)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('keep')")
        conn.commit()
        conn.close()
        dest_dir = Path(tmp.name) / "out"
        os.environ["LIVE402_CATALOG_DB"] = str(src)
        os.environ["LIVE402_HISTORY_DB"] = str(Path(tmp.name) / "missing-history.sqlite")
        os.environ["LIVE402_PQ_LOG_DB"] = str(Path(tmp.name) / "missing-pq.sqlite")
        try:
            rc = backup_sqlite.main(["--dest", str(dest_dir)])
            self.assertEqual(rc, 1)
            snaps = list(dest_dir.glob("catalog-*.sqlite"))
            self.assertEqual(len(snaps), 1)
            restored = Path(tmp.name) / "restored.sqlite"
            self.assertEqual(restore_sqlite.main(["--src", str(snaps[0]), "--dest", str(restored)]), 0)
            conn = sqlite3.connect(restored)
            self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "keep")
            conn.close()
        finally:
            os.environ.pop("LIVE402_CATALOG_DB", None)
            os.environ.pop("LIVE402_HISTORY_DB", None)
            os.environ.pop("LIVE402_PQ_LOG_DB", None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
