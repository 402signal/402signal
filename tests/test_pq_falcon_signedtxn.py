"""Falcon SignedTxn pqsig decode + envelope. No live network. No broadcast."""

from __future__ import annotations

import base64
import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import ORIGIN, algo_anchor
from tests.pq_test_env import clear_pq_env


_ADDR = payment.DEFAULT_PAYTO_ALGORAND
_ROOT = b"\x11" * 32
# High-bit payloads: not valid UTF-8, so go-algorand Raw str16 stays bytes.
_PK = bytes((0x80 + (i % 0x40)) for i in range(algo_anchor.FALCON_F1_PK_LEN))
_SIG = bytes((0xC0 + (i % 0x20)) for i in range(320))


def _pay_txn(*, size=1, root=_ROOT, network="testnet"):
    note = algo_anchor.encode_note(ORIGIN, int(size), bytes(root))
    if network == "mainnet":
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        gen = algo_anchor.MAINNET_GENESIS_ID
    else:
        gh = base64.b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        gen = algo_anchor.TESTNET_GENESIS_ID
    return algo_tx.pay_txn(_ADDR, _ADDR, 0, 3000, 1, 1001, gen, gh, note=note)


def _envelope(*, sch="f1", pk=_PK, sig=_SIG, slt=0, txn=None, **extra):
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
        env = _envelope(sch="f1")
        env["pqsig"].pop("slt")
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
