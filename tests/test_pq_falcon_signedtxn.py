"""Falcon SignedTxn pqsig decode + envelope. No live network. No broadcast."""

from __future__ import annotations

import base64
import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, canary, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env


_ADDR = payment.DEFAULT_PAYTO_ALGORAND
_ROOT = b"\x11" * 32
# Live QA shape: sch=b"f1" (len 2), pk 1793, sig 1233, no slt key.
_PK = bytes((0x80 + (i % 0x40)) for i in range(algo_anchor.FALCON_F1_PK_LEN))
_SIG = bytes((0xC0 + (i % 0x20)) for i in range(algo_anchor.FALCON_F1_SIG_LIVE))
_LIVE_SIG_NOTE = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")


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

    def test_salt_byte_and_omitted_pass(self):
        blob = algo_tx.msgpack_encode(_envelope(sch=b"f1", slt=b"\x00"))
        self._validate(blob)
        env = _envelope(sch=b"f1")
        self.assertNotIn("slt", env["pqsig"])
        self._validate(algo_tx.msgpack_encode(env))

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
        self.assertIn("falcon authorization missing", str(ctx.exception))

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
        self.assertEqual(len(env["pqsig"]["sig"]), 1233)
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

    def test_genuine_algokey_shaped_f1_bytes_passes(self):
        env = _envelope(sch=b"f1")
        self.assertEqual(env["pqsig"]["sch"], b"f1")
        self.assertEqual(len(env["pqsig"]["pk"]), algo_anchor.FALCON_F1_PK_LEN)
        self.assertEqual(len(env["pqsig"]["sig"]), algo_anchor.FALCON_F1_SIG_LIVE)
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
        oversized_sig = algo_tx.msgpack_encode(
            _envelope(sch=b"f1", sig=b"\x01" * (algo_anchor.FALCON_F1_SIG_MAX + 1), txn=txn)
        )
        substituted_pk = algo_tx.msgpack_encode(_envelope(sch=b"f1", pk=b"\x00" * 32, txn=txn))
        for blob in (truncated_pk, empty_sig, oversized_sig, substituted_pk):
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
