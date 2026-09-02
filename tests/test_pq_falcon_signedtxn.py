"""Falcon SignedTxn pqsig decode + envelope. No live network. No broadcast."""

from __future__ import annotations

import base64
import hashlib
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, canary, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env


_ADDR = payment.DEFAULT_PAYTO_ALGORAND
_ROOT = b"\x11" * 32
# Synthetic algokey shape: sch=b"f1" (len 2), pk 1793, compressed sig, no slt.
_PK = bytes((0x80 + (i % 0x40)) for i in range(algo_anchor.FALCON_F1_PK_LEN))
_SIG = b"\xba\x00" + bytes((0xC0 + (i % 0x20)) for i in range(algo_anchor.FALCON_F1_SIG_FIXTURE - 2))
_LIVE_SIG_NOTE = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_TREE4_TXID = "HIQM6VWDMUWHUTQG7SZF2QW3XYFY4HRLRK3BV22CQLENDRB7AKJQ"
_TREE4_ADDRESS = "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU"
_TREE4_FIXTURE = Path(__file__).parent / "fixtures" / "pq_falcon_wire" / "tree4_signedtxn.b64"


def _pay_txn(*, size=1, root=_ROOT, network="testnet"):
    note = algo_anchor.encode_note(ORIGIN, int(size), bytes(root))
    if network == "mainnet":
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        gen = algo_anchor.MAINNET_GENESIS_ID
    else:
        gh = base64.b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        gen = algo_anchor.TESTNET_GENESIS_ID
    return algo_tx.pay_txn(_ADDR, _ADDR, 0, 3000, 1, 1001, gen, gh, note=note)


def _envelope(*, sch="f1", pk=_PK, sig=_SIG, slt=None, txn=None, **extra):
    pqsig = {"pk": pk, "sch": sch, "sig": sig}
    if slt is not None:
        pqsig["slt"] = slt
    pqsig.update(extra)
    return {"pqsig": pqsig, "txn": txn if txn is not None else _pay_txn()}


def _retag_bin_as_str(blob: bytes, payload: bytes) -> bytes:
    """Rewrite one bin8/16/32 as the matching msgpack str (go-codec Raw)."""
    n = len(payload)
    if n < 256:
        old = b"\xc4" + bytes([n]) + payload
        new = b"\xd9" + bytes([n]) + payload
    elif n < 65536:
        old = b"\xc5" + n.to_bytes(2, "big") + payload
        new = b"\xda" + n.to_bytes(2, "big") + payload
    else:
        old = b"\xc6" + n.to_bytes(4, "big") + payload
        new = b"\xdb" + n.to_bytes(4, "big") + payload
    if old not in blob:
        raise AssertionError("bin payload not found")
    return blob.replace(old, new, 1)


def _map1(tag: bytes) -> bytes:
    return b"\x81" + b"\xa1v" + tag


class MsgpackAlgorandTypesTests(unittest.TestCase):
    def test_nil_signed_ints_array16_map32_str16(self):
        self.assertEqual(algo_tx.msgpack_decode(_map1(b"\xc0")), {"v": None})
        self.assertEqual(algo_tx.msgpack_decode(_map1(b"\xff")), {"v": -1})
        self.assertEqual(algo_tx.msgpack_decode(_map1(b"\xe0")), {"v": -32})
        self.assertEqual(algo_tx.msgpack_decode(_map1(b"\xd0\x80")), {"v": -128})
        self.assertEqual(algo_tx.msgpack_decode(_map1(b"\xd1\xff\x00")), {"v": -256})
        self.assertEqual(
            algo_tx.msgpack_decode(_map1(b"\xd2\xff\xff\xff\x00")),
            {"v": -256},
        )
        self.assertEqual(
            algo_tx.msgpack_decode(_map1(b"\xd3" + (0).to_bytes(8, "big", signed=True))),
            {"v": 0},
        )
        raw16 = bytes((0x80 + (i % 0x40)) for i in range(300))
        blob = _map1(b"\xda" + (300).to_bytes(2, "big") + raw16)
        self.assertEqual(algo_tx.msgpack_decode(blob), {"v": raw16})
        raw32 = bytes((0x81, 0x82, 0x83))
        blob = _map1(b"\xdb" + (3).to_bytes(4, "big") + raw32)
        self.assertEqual(algo_tx.msgpack_decode(blob), {"v": raw32})
        blob = b"\xdf" + (1).to_bytes(4, "big") + b"\xa1a" + b"\x01"
        self.assertEqual(algo_tx.msgpack_decode(blob), {"a": 1})
        items = [bytes([i]) for i in range(16)]
        encoded = algo_tx.msgpack_encode({"txlist": items})
        self.assertIn(b"\xdc", encoded)
        self.assertEqual(algo_tx.msgpack_decode(encoded)["txlist"], items)
        arr32 = b"\xdd" + (1).to_bytes(4, "big") + b"\x05"
        self.assertEqual(algo_tx.msgpack_decode(_map1(arr32)), {"v": [5]})

    def test_round_trip_and_str16_binary_inside_map(self):
        src = {"k": "f1", "n": 0, "raw": _PK[:64]}
        self.assertEqual(algo_tx.msgpack_decode(algo_tx.msgpack_encode(src)), src)


class FalconSignedTxnEnvelopeTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _ADDR
        self.addCleanup(clear_pq_env)

    def _validate(self, blob, *, size=1, root=_ROOT):
        return algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN,
            expected_size=size,
            expected_root=root,
            expected_address=_ADDR,
            expected_network="testnet",
        )

    def test_encode_decode_falcon_envelope_passes(self):
        blob = algo_tx.msgpack_encode(_envelope(sch="f1"))
        again = algo_tx.msgpack_decode(blob)
        self.assertEqual(again["pqsig"]["sch"], "f1")
        self.assertEqual(again["pqsig"]["pk"], _PK)
        self.assertEqual(again["pqsig"]["sig"], _SIG)
        out = self._validate(blob)
        self.assertEqual(out["tree_size"], 1)
        self.assertEqual(out["address"], _ADDR)

    def test_algokey_sch_bytes_f1_passes(self):
        """Live algokey/msgp encodes PQScheme [2]byte as bin b'f1'."""
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1"))
        decoded = algo_tx.msgpack_decode(blob)
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertNotEqual(str(decoded["pqsig"]["sch"]), "f1")
        out = self._validate(blob)
        self.assertEqual(out["tree_size"], 1)

    def test_go_algorand_str16_pk_sig_passes(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1"))
        blob = _retag_bin_as_str(blob, _PK)
        blob = _retag_bin_as_str(blob, _SIG)
        self.assertIn(b"\xda", blob)
        decoded = algo_tx.msgpack_decode(blob)
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertEqual(decoded["pqsig"]["pk"], _PK)
        self.assertEqual(decoded["pqsig"]["sig"], _SIG)
        out = self._validate(blob)
        self.assertEqual(out["tree_size"], 1)

    def test_canonical_salt_integer_and_omitted_pass(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", slt=0))
        self._validate(blob)
        env = _envelope(sch=b"f1")
        self.assertNotIn("slt", env["pqsig"])
        self._validate(algo_tx.msgpack_encode(env))

    def test_noncanonical_salt_byte_fails_signedtxn_validation(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", slt=b"\x00"))
        with self.assertRaises(algo_anchor.AnchorError):
            self._validate(blob)

    def test_missing_pqsig_fails_distinct_error(self):
        blob = algo_tx.msgpack_encode({"txn": _pay_txn()})
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            self._validate(blob)
        self.assertEqual(str(ctx.exception), "falcon authorization missing: no pqsig key")

    def test_wrong_scheme_fails_bad_envelope(self):
        for sch in ("f5", b"f5", "ed25519", b""):
            blob = algo_tx.msgpack_encode(_envelope(sch=sch))
            with self.assertRaises(algo_anchor.AnchorError) as ctx:
                self._validate(blob)
            self.assertEqual(
                str(ctx.exception),
                "falcon authorization missing: bad pqsig envelope",
            )

    def test_marker_only_is_not_authorization(self):
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            self._validate(b"present")
        self.assertIn("marker", str(ctx.exception))
        blob = algo_tx.msgpack_encode({"pqsig": "present", "txn": _pay_txn()})
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            self._validate(blob)
        self.assertEqual(
            str(ctx.exception),
            "falcon authorization missing: bad pqsig envelope",
        )

    def test_empty_pqsig_and_bare_blob_fail(self):
        blob = algo_tx.msgpack_encode({"pqsig": {}, "txn": _pay_txn()})
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            self._validate(blob)
        self.assertEqual(
            str(ctx.exception),
            "falcon authorization missing: bad pqsig envelope",
        )
        blob = algo_tx.msgpack_encode({"pqsig": _SIG, "txn": _pay_txn()})
        with self.assertRaises(algo_anchor.AnchorError):
            self._validate(blob)

    def test_ed25519_sig_fails(self):
        blob = algo_tx.msgpack_encode({"sig": b"\x11" * 64, "txn": _pay_txn()})
        with self.assertRaises(algo_anchor.AnchorError):
            self._validate(blob)

    def test_signature_falcon_bare_blob_fails(self):
        blob = algo_tx.msgpack_encode(
            {"signature": {"falcon": _SIG}, "txn": _pay_txn()}
        )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            self._validate(blob)
        err = str(ctx.exception)
        self.assertTrue(
            "unknown SignedTxn field" in err or "falcon authorization missing" in err,
            err,
        )

    def test_ipc_marker_present_is_not_auth(self):
        self.assertIsNone(
            algo_anchor._pq_auth_from_obj({"pqsig": algo_anchor.PQSIG_MARKER})
        )
        self.assertIsNone(
            algo_anchor._parse_pqsig_envelope(
                {"sch": "f1", "pk": _PK, "sig": "present", "slt": 0}
            )
        )

    def test_real_f1_binary_scheme_live_shape_passes(self):
        """REAL_F1_BINARY_SCHEME: live algokey keys, no slt, sch=b'f1'."""
        env = _envelope(sch=b"f1")
        self.assertEqual(set(env), {"pqsig", "txn"})
        self.assertEqual(set(env["pqsig"]), {"pk", "sch", "sig"})
        self.assertEqual(env["pqsig"]["sch"], b"f1")
        self.assertEqual(len(env["pqsig"]["sch"]), 2)
        self.assertEqual(len(env["pqsig"]["pk"]), 1793)
        self.assertEqual(len(env["pqsig"]["sig"]), algo_anchor.FALCON_F1_SIG_FIXTURE)
        blob = algo_tx.msgpack_encode(env)
        decoded = algo_tx.msgpack_decode(blob)
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertEqual(str(decoded["pqsig"]["sch"]), "b'f1'")
        self.assertEqual(decoded["pqsig"]["sch"], algo_anchor.PQSIG_WIRE_SCH)
        out = self._validate(blob)
        self.assertEqual(out["tree_size"], 1)

    def test_invalid_schemes_fail_closed(self):
        cases = (
            b"f5",
            b"F1",
            b"f1 ",
            b"",
            "F1",
            "f1 ",
            "b'f1'",
            None,
        )
        for sch in cases:
            env = _envelope()
            if sch is None:
                env["pqsig"].pop("sch")
            else:
                env["pqsig"]["sch"] = sch
            blob = algo_tx.msgpack_encode(env)
            with self.assertRaises(algo_anchor.AnchorError) as ctx:
                self._validate(blob)
            self.assertIn("falcon authorization missing", str(ctx.exception))
        self.assertIsNone(algo_anchor._parse_pqsig_envelope({"sch": b"\xff\xfe", "pk": _PK, "sig": _SIG}))
        self.assertIsNone(algo_anchor._parse_pqsig_envelope({"sch": 1, "pk": _PK, "sig": _SIG}))
        self.assertIsNone(algo_anchor._parse_pqsig_envelope({"sch": ["f", "1"], "pk": _PK, "sig": _SIG}))
        self.assertIsNone(algo_anchor._parse_pqsig_envelope({"sch": {"f1": True}, "pk": _PK, "sig": _SIG}))

    def test_pqsig_preserved_after_validate(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1"))
        before = algo_tx.msgpack_decode(blob)
        self._validate(blob)
        after = algo_tx.msgpack_decode(blob)
        self.assertEqual(after["pqsig"], before["pqsig"])
        self.assertEqual(after["pqsig"]["sch"], b"f1")
        self.assertEqual(after["pqsig"]["pk"], _PK)
        self.assertEqual(after["pqsig"]["sig"], _SIG)
        self.assertNotIn("slt", after["pqsig"])


class AuthorizedPersistenceAndResumeTests(unittest.TestCase):
    """receive → validate → persist AUTHORIZED → return. No POST."""

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = _ADDR
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        store.reset()
        store.append(b"tree3-leaf")
        self.root = store.root(1)
        body = ckpt.checkpoint_body(ORIGIN_MAINNET, 1, self.root)
        store.save_checkpoint(1, "%s\n%s %s %s\n" % (body, ckpt.EMDASH, ORIGIN_MAINNET, _LIVE_SIG_NOTE))
        note = algo_anchor.encode_note(ORIGIN_MAINNET, 1, self.root)
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(_ADDR, _ADDR, 0, 3000, 1, 1001, algo_anchor.MAINNET_GENESIS_ID, gh, note=note)
        self.blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
        self.params = {
            "minFee": 1000,
            "fee": 0,
            "lastRound": 1,
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_crash_before_authorized_leaves_no_row(self):
        algo_anchor.validate_signed_txn(
            self.blob,
            expected_origin=ORIGIN_MAINNET,
            expected_size=1,
            expected_root=self.root,
            expected_address=_ADDR,
            expected_network="mainnet",
            params=self.params,
            require_canonical=True,
        )
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_crash_after_authorized_row_survives(self):
        row = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        self.assertEqual(canary.send_state_of(row), canary.STATE_AUTHORIZED)
        self.assertEqual(bytes(row["signed"]), self.blob)
        again = store.authorized_at(1)
        self.assertEqual(bytes(again["signed"]), self.blob)
        self.assertEqual(canary.send_state_of(again), canary.STATE_AUTHORIZED)

    def test_restart_after_authorized_reuses_blob(self):
        canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        restarted = canary.authorize(
            params=self.params,
            sign_fn=lambda _i: (_ for _ in ()).throw(AssertionError("must not re-sign")),
        )
        self.assertEqual(bytes(restarted["signed"]), self.blob)
        self.assertEqual(canary.send_state_of(restarted), canary.STATE_AUTHORIZED)

    def test_duplicate_prepare_does_not_sign_again(self):
        signed = []
        first = canary.authorize(params=self.params, sign_fn=lambda _i: signed.append(1) or self.blob)
        second = canary.authorize(params=self.params, sign_fn=lambda _i: signed.append(1) or self.blob)
        self.assertEqual(len(signed), 1)
        self.assertEqual(bytes(first["signed"]), bytes(second["signed"]))
        self.assertEqual(bytes(first["signed"]), self.blob)

    def test_tree3_resume_creates_zero_new_signatures(self):
        signed = []
        row = canary.authorize(params=self.params, sign_fn=lambda _i: signed.append("new") or self.blob)
        again = canary.authorize(params=self.params, sign_fn=lambda _i: signed.append("new") or self.blob)
        self.assertEqual(signed, ["new"])
        self.assertEqual(bytes(row["signed"]), bytes(again["signed"]))
        decoded = algo_tx.msgpack_decode(bytes(row["signed"]))
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertNotIn("slt", decoded["pqsig"])

    def test_persist_existing_signed_never_authorizes(self):
        with self.assertRaises(canary.CanarySecurityError):
            canary.persist_existing_signed(self.blob, params=self.params)
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_persist_authorized_rejects_forgeable_provenance_values(self):
        class EqualToEverything:
            def __eq__(self, _other):
                return True

        for forged in (None, True, "fixture-sign-hook", "response-mac", object(), EqualToEverything()):
            with self.subTest(forged=repr(forged)):
                with mock.patch.object(
                    store,
                    "save_authorized_checkpoint",
                    side_effect=AssertionError("must not write"),
                ) as save:
                    with self.assertRaises(canary.CanarySecurityError):
                        canary.persist_authorized(
                            tree_size=1,
                            origin=ORIGIN_MAINNET,
                            root=self.root,
                            checkpoint="fixture",
                            request_id="fixture",
                            signed=self.blob,
                            at=1,
                            _capability=forged,
                        )
                    save.assert_not_called()

    def test_invalid_envelope_does_not_persist_authorized(self):
        bad = algo_tx.msgpack_encode(_envelope(sch=b"f5", txn=algo_tx.msgpack_decode(self.blob)["txn"]))
        with self.assertRaises((algo_anchor.AnchorError, canary.CanaryError)):
            canary.authorize(params=self.params, sign_fn=lambda _i: bad)
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_sign_hook_forbidden_outside_fixture(self):
        os.environ.pop("LIVE402_FIXTURE", None)
        os.environ.pop("LIVE402_PQ_TEST_SUPPORT", None)
        try:
            with self.assertRaises(canary.CanarySecurityError):
                canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
            self.assertIsNone(store.authorized_at(1))
        finally:
            os.environ["LIVE402_FIXTURE"] = "1"

    def test_production_authenticated_reply_persists_authorized(self):
        reply = {
            "signed": self.blob,
            "verified": {"fee": 3000, "fv": 1, "lv": 1001},
            "response_authenticated": True,
        }
        with mock.patch("live402.pq.signer_mainnet.request_signed", return_value=reply) as request_signed:
            row = canary.authorize(params=self.params, request_id="authenticated")
            request_signed.assert_called_once()
        self.assertEqual(canary.send_state_of(row), canary.STATE_AUTHORIZED)
        self.assertEqual(bytes(row["signed"]), self.blob)

    def test_production_missing_response_authentication_does_not_persist(self):
        reply = {
            "signed": self.blob,
            "verified": {"fee": 3000, "fv": 1, "lv": 1001},
        }
        with mock.patch("live402.pq.signer_mainnet.request_signed", return_value=reply) as request_signed:
            with self.assertRaises(canary.CanarySecurityError) as ctx:
                canary.authorize(params=self.params)
            self.assertIn("provenance", str(ctx.exception))
            request_signed.assert_called_once()
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_read_only_inspect_does_not_persist(self):
        out = canary.inspect(params=self.params)
        self.assertTrue(out.get("read_only"))
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_prepare_fail_closes_on_unauthenticated_response(self):
        with mock.patch(
            "live402.pq.signer_mainnet.request_signed",
            return_value={"signed": self.blob, "verified": {"fee": 3000, "fv": 1, "lv": 1001}},
        ) as request_signed:
            with self.assertRaises(canary.CanarySecurityError) as ctx:
                canary.prepare(params=self.params)
            self.assertIn("provenance", str(ctx.exception))
            request_signed.assert_called_once()
        self.assertIsNone(store.authorized_at(1))
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))


class FalconShapeAndSchemeMatrixTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _ADDR
        self.addCleanup(clear_pq_env)

    def _validate(self, blob, *, size=1, root=_ROOT):
        return algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN,
            expected_size=size,
            expected_root=root,
            expected_address=_ADDR,
            expected_network="testnet",
        )

    def test_local_encoder_algokey_shaped_f1_bytes_passes(self):
        """Local encoder + synthetic pk/sig. Not byte-for-byte live algokey wire."""
        env = _envelope(sch=b"f1")
        self.assertEqual(env["pqsig"]["sch"], b"f1")
        self.assertEqual(len(env["pqsig"]["pk"]), algo_anchor.FALCON_F1_PK_LEN)
        self.assertEqual(len(env["pqsig"]["sig"]), algo_anchor.FALCON_F1_SIG_FIXTURE)
        blob = algo_tx.msgpack_encode(env)
        decoded = algo_tx.msgpack_decode(blob, strict=True)
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        out = self._validate(blob)
        self.assertEqual(out["tree_size"], 1)
        self.assertEqual(out["txid"], algo_anchor.signed_txn_txid(blob))

    def test_malformed_scheme_matrix(self):
        cases = (
            b"F1",
            b"f5",
            b"f1\x00",
            b"f1 ",
            b" f1",
            b"",
            "F1",
            "f5",
            "f1 ",
            "b'f1'",
            "f1\n",
            1,
            ["f", "1"],
            {"sch": "f1"},
            None,
        )
        for sch in cases:
            env = _envelope()
            if sch is None:
                env["pqsig"].pop("sch")
            else:
                env["pqsig"]["sch"] = sch
            if sch in (1, ["f", "1"], {"sch": "f1"}):
                self.assertIsNone(algo_anchor._parse_pqsig_envelope(env["pqsig"]))
                continue
            blob = algo_tx.msgpack_encode(env)
            with self.assertRaises(algo_anchor.AnchorError) as ctx:
                self._validate(blob)
            self.assertIn("falcon authorization missing", str(ctx.exception))

    def test_truncated_mutated_substituted_pk_sig(self):
        txn = _pay_txn()
        truncated_pk = algo_tx.msgpack_encode(_envelope(sch=b"f1", pk=_PK[:-1], txn=txn))
        empty_sig = algo_tx.msgpack_encode(_envelope(sch=b"f1", sig=b"", txn=txn))
        one_byte_sig = algo_tx.msgpack_encode(_envelope(sch=b"f1", sig=b"\x01", txn=txn))
        oversized_sig = algo_tx.msgpack_encode(
            _envelope(sch=b"f1", sig=b"\x01" * (algo_anchor.FALCON_F1_SIG_MAX + 1), txn=txn)
        )
        substituted_pk = algo_tx.msgpack_encode(_envelope(sch=b"f1", pk=b"\x00" * 32, txn=txn))
        for blob in (truncated_pk, empty_sig, one_byte_sig, oversized_sig, substituted_pk):
            with self.assertRaises(algo_anchor.AnchorError) as ctx:
                self._validate(blob)
            self.assertIn("falcon authorization missing", str(ctx.exception))
        mutated = _envelope(sch=b"f1", pk=bytes([_PK[0] ^ 0xFF]) + _PK[1:], txn=txn)
        blob = algo_tx.msgpack_encode(mutated)
        before = algo_tx.msgpack_decode(blob, strict=True)["pqsig"]
        self._validate(blob)
        after = algo_tx.msgpack_decode(blob, strict=True)["pqsig"]
        self.assertEqual(after, before)
        self.assertEqual(after["pk"], mutated["pqsig"]["pk"])
        self.assertNotEqual(after["pk"], _PK)


class ConfirmedMainNetWireFixtureTests(unittest.TestCase):
    def test_tree4_algokey_signedtxn_exact_wire_passes(self):
        """Offline public-chain fixture: genuine b'f1', pk, sig and txn wire."""
        encoded = b"".join(_TREE4_FIXTURE.read_bytes().split())
        blob = base64.b64decode(encoded, validate=True)
        self.assertEqual(len(blob), 3310)
        self.assertEqual(
            hashlib.sha256(blob).hexdigest(),
            "566db3b3efd9db449e5f62e36b7986bcfa87e7875cc3ae1b53605063ae570af3",
        )
        decoded = algo_tx.msgpack_decode(blob, strict=True)
        self.assertEqual(list(decoded), ["pqsig", "txn"])
        self.assertEqual(list(decoded["pqsig"]), ["pk", "sch", "sig"])
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertEqual(len(decoded["pqsig"]["pk"]), 1793)
        self.assertEqual(len(decoded["pqsig"]["sig"]), 1230)
        self.assertEqual(decoded["pqsig"]["sig"][:2], b"\xba\x00")

        note = algo_anchor.decode_note(decoded["txn"]["note"])
        self.assertEqual(note["tree_size"], 4)
        out = algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN_MAINNET,
            expected_size=4,
            expected_root=note["root"],
            expected_address=_TREE4_ADDRESS,
            expected_network="mainnet",
            require_canonical=False,
        )
        self.assertEqual(out["txid"], _TREE4_TXID)
        self.assertEqual(out["fee"], 3000)


class StrictMsgpackSignedTxnTests(unittest.TestCase):
    def test_duplicate_key_rejected(self):
        inner = b"\xa1v" + b"\x01" + b"\xa1v" + b"\x02"
        blob = b"\x82" + inner
        with self.assertRaises(ValueError):
            algo_tx.msgpack_decode(blob)
        with self.assertRaises(ValueError):
            algo_tx.msgpack_decode(blob, strict=True)

    def test_negative_int_rejected_in_strict(self):
        blob = b"\x81" + b"\xa3fee" + b"\xff"
        self.assertEqual(algo_tx.msgpack_decode(blob)["fee"], -1)
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("negative", str(ctx.exception))

    def test_non_minimal_int_rejected_in_strict(self):
        blob = b"\x81" + b"\xa1n" + b"\xcc\x01"
        self.assertEqual(algo_tx.msgpack_decode(blob)["n"], 1)
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("non-minimal", str(ctx.exception))

    def test_trailing_data_rejected(self):
        blob = b"\x81" + b"\xa1n" + b"\x01" + b"\x00"
        with self.assertRaises(ValueError):
            algo_tx.msgpack_decode(blob)
        with self.assertRaises(ValueError):
            algo_tx.msgpack_decode(blob, strict=True)

    def test_unproven_forms_rejected_in_strict(self):
        map32 = b"\xdf" + (1).to_bytes(4, "big") + b"\xa1a" + b"\x01"
        self.assertEqual(algo_tx.msgpack_decode(map32), {"a": 1})
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(map32, strict=True)
        self.assertIn("unproven", str(ctx.exception))

    def test_excessive_nesting_rejected(self):
        blob = b"\x81\xa1n\x01"
        for _ in range(algo_tx._MAX_MSGPACK_DEPTH + 2):
            blob = b"\x81\xa1n" + blob
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("nesting", str(ctx.exception))

    def test_strict_accepts_encoder_algokey_shape_without_reencode_eq(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1"))
        decoded = algo_tx.msgpack_decode(blob, strict=True)
        self.assertEqual(decoded["pqsig"]["sch"], b"f1")
        self.assertEqual(decoded["pqsig"]["pk"], _PK)
        retagged = _retag_bin_as_str(blob, _PK)
        retagged = _retag_bin_as_str(retagged, _SIG)
        again = algo_tx.msgpack_decode(retagged, strict=True)
        self.assertEqual(again["pqsig"]["pk"], _PK)
        self.assertEqual(again["pqsig"]["sig"], _SIG)
        self.assertNotEqual(retagged, blob)

    def test_validate_rejects_duplicate_and_negative(self):
        good = algo_tx.msgpack_encode(_envelope(sch=b"f1"))
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                good + b"\x00",
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )

    def test_strict_rejects_bool_and_nil(self):
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(_map1(b"\xc2"), strict=True)
        self.assertIn("bool", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(_map1(b"\xc0"), strict=True)
        self.assertIn("nil", str(ctx.exception))

    def test_amt_false_rejected_as_boolean_integer(self):
        txn = _pay_txn()
        txn["amt"] = False
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("bool", str(ctx.exception))
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor._require_uint(False)
        self.assertIn("boolean-as-integer", str(ctx.exception))

    def test_fee_fv_lv_salt_booleans_rejected(self):
        for key in ("fee", "fv", "lv"):
            with self.subTest(key=key):
                txn = _pay_txn()
                txn[key] = False
                blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
                with self.assertRaises(ValueError) as ctx:
                    algo_tx.msgpack_decode(blob, strict=True)
                self.assertIn("bool", str(ctx.exception))
                with self.assertRaises(algo_anchor.AnchorError):
                    algo_anchor.validate_signed_txn(
                        blob,
                        expected_origin=ORIGIN,
                        expected_size=1,
                        expected_root=_ROOT,
                        expected_address=_ADDR,
                        expected_network="testnet",
                    )
                with self.assertRaises(algo_anchor.AnchorError) as ctx:
                    algo_anchor._require_uint(False)
                self.assertIn("boolean-as-integer", str(ctx.exception))
        env = _envelope(sch=b"f1", slt=False)
        blob = algo_tx.msgpack_encode(env)
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("bool", str(ctx.exception))
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertFalse(algo_anchor._salt_in_range(False))
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor._require_uint(False)

    def test_unordered_keys_rejected_in_strict(self):
        """Descending keys fail closed. Not a claim of official algokey canonical order."""
        blob = b"\x82" + b"\xa1z" + b"\x01" + b"\xa1a" + b"\x02"
        decoded = algo_tx.msgpack_decode(blob)
        self.assertEqual(decoded, {"z": 1, "a": 2})
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("unordered", str(ctx.exception))

    def test_non_text_key_rejected_in_strict(self):
        blob = b"\x81" + b"\x01" + b"\x02"
        self.assertEqual(algo_tx.msgpack_decode(blob), {"1": 2})
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("non-text", str(ctx.exception))

    def test_non_minimal_map16_rejected_in_strict(self):
        blob = b"\xde\x00\x01" + b"\xa1a" + b"\x01"
        self.assertEqual(algo_tx.msgpack_decode(blob), {"a": 1})
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("non-minimal", str(ctx.exception))

    def test_non_minimal_array16_rejected_in_strict(self):
        blob = _map1(b"\xdc\x00\x01\x01")
        self.assertEqual(algo_tx.msgpack_decode(blob), {"v": [1]})
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(blob, strict=True)
        self.assertIn("non-minimal", str(ctx.exception))

    def test_non_bytes_decoder_input_rejected_before_coercion(self):
        for raw in (8192, object(), "\x80"):
            with self.assertRaises(TypeError):
                algo_tx.msgpack_decode(raw, strict=True)

    def test_explicit_zero_amount_is_noncanonical(self):
        txn = _pay_txn()
        txn["amt"] = 0
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertIn("canonical zero", str(ctx.exception))

    def test_required_wire_field_types_fail_closed(self):
        cases = (
            ("fee", "3000"),
            ("fv", "1"),
            ("lv", "1001"),
            ("snd", "not-wire-bytes"),
            ("rcv", "not-wire-bytes"),
            ("note", "00"),
            ("gh", "not-wire-bytes"),
        )
        for key, value in cases:
            with self.subTest(key=key):
                txn = _pay_txn()
                txn[key] = value
                blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
                with self.assertRaises(algo_anchor.AnchorError):
                    algo_anchor.validate_signed_txn(
                        blob,
                        expected_origin=ORIGIN,
                        expected_size=1,
                        expected_root=_ROOT,
                        expected_address=_ADDR,
                        expected_network="testnet",
                    )

    def test_signature_header_and_salt_version_fail_closed(self):
        for sig in (b"\x00\x00" + _SIG[2:], b"\xba\x01" + _SIG[2:]):
            self.assertFalse(algo_anchor._falcon_f1_shapes_ok(_PK, sig))
            blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", sig=sig))
            with self.assertRaises(algo_anchor.AnchorError):
                algo_anchor.validate_signed_txn(
                    blob,
                    expected_origin=ORIGIN,
                    expected_size=1,
                    expected_root=_ROOT,
                    expected_address=_ADDR,
                    expected_network="testnet",
                )

    def test_unknown_signedtxn_and_txn_fields_rejected(self):
        extra = _envelope(sch=b"f1")
        extra["zzz"] = 1
        blob = algo_tx.msgpack_encode(extra)
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertIn("unknown SignedTxn field", str(ctx.exception))
        txn = _pay_txn()
        txn["zzz"] = 1
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", txn=txn))
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertIn("unknown txn field", str(ctx.exception))
        env = _envelope(sch=b"f1")
        env["pqsig"]["zzz"] = 1
        blob = algo_tx.msgpack_encode(env)
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertIn("unknown pqsig field", str(ctx.exception))

    def test_one_byte_sig_rejected(self):
        """Structural parser bound: header+salt-version need 2 bytes. Not Falcon verify."""
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", sig=b"\x01"))
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=_ROOT,
                expected_address=_ADDR,
                expected_network="testnet",
            )
        self.assertIn("falcon authorization missing", str(ctx.exception))
        self.assertFalse(algo_anchor._falcon_f1_shapes_ok(_PK, b"\x01"))
        self.assertEqual(algo_anchor.FALCON_F1_SIG_MIN, 2)
        self.assertLess(algo_anchor.FALCON_F1_SIG_MIN, algo_anchor.FALCON_F1_SIG_FIXTURE)

    def test_raw_byte_size_cap(self):
        with self.assertRaises(ValueError) as ctx:
            algo_tx.msgpack_decode(b"\x80" + b"\x00" * algo_tx.MAX_MSGPACK_BYTES, strict=True)
        self.assertIn("too large", str(ctx.exception))
        with self.assertRaises(ValueError):
            algo_tx.msgpack_decode(b"\x00" * (algo_tx.MAX_MSGPACK_BYTES + 1))


class CeremonyMetadataNotConfirmationTests(unittest.TestCase):
    def test_trust_root_ceremony_is_not_confirmed_or_anchored(self):
        from live402.pq import trust, transparency, worker

        tmp = __import__("tempfile").TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(tmp.name, "pq-log-mainnet.sqlite")
        store.reset()
        try:
            desc = trust.trust_root_v2()
            ceremony = str((desc.get("falcon") or {}).get("ceremony") or "")
            self.assertTrue(ceremony)
            self.assertIn("canary-confirmed", ceremony)
            self.assertFalse(worker.public_anchor())
            self.assertEqual(int(store.last_confirmed_checkpoint().get("size") or 0), 0)
            html = transparency.homepage_pq_html()
            self.assertNotIn("Anchored", html)
            self.assertNotEqual(html, transparency.HOMEPAGE_ANCHORED_CHIP)
        finally:
            store.reset()
            clear_pq_env()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
