"""Offline pq-anchor/2 cross-repo wire contract (router serialization gate).

Builds real request bytes via narrow_policy / build_request /
encode_request_line. Pins a Go-compatible fixture corpus Ross can
mirror on private signer CI.

This is not a live Go parser test. Full Go decode + HMAC-verify runs
on signer CI under Ross. No extra GitHub secret. No MainNet dial.
No Falcon SK. CROSS_REPO_EXTRA_SECRET_REQUIRED=NO.
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import ORIGIN_MAINNET, signer_mainnet
from live402.pq import checkpoint as ckpt

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "pq_anchor2_wire"
FIXTURE_TOKEN = "fixture-hmac-token-not-a-secret"
_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_GOLDEN_ROOT = "abababababababababababababababababababababababababababababababab"
_POLICY = {
    "canonical_fee": 3000,
    "fee_per_byte": 0,
    "fv": 10,
    "last_round": 10,
    "lv": 1010,
    "min_fee": 1000,
    "size_rule": "deterministic_falcon_envelope_estimate",
    "size_version": "1",
    "snapshot_at": 1700000000,
}


def _signed_note(size, root, origin=ORIGIN_MAINNET):
    body = ckpt.checkpoint_body(origin, int(size), bytes(root))
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, origin, _SIG)


class CrossRepoWireContractTests(unittest.TestCase):
    def test_readme_documents_ross_only_go_ci(self):
        text = (CORPUS / "README.md").read_text(encoding="utf-8")
        self.assertIn("Ross", text)
        self.assertIn("private signer CI", text)
        self.assertIn("CROSS_REPO_EXTRA_SECRET_REQUIRED=NO", text)
        self.assertIn("router serialization gate", text)
        self.assertIn("Do not add a GitHub secret", text)
        manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["extra_secret_required"], False)
        self.assertEqual(manifest["go_parser_ci"], "ross-only-private-signer")
        self.assertEqual(manifest["network"], "none")
        self.assertEqual(manifest["size_version_json_type"], "string")

    def test_narrow_policy_rejects_int_size_version(self):
        bad = dict(_POLICY)
        bad["size_version"] = 1
        with self.assertRaises(signer_mainnet.SignerClientError) as ctx:
            signer_mainnet.narrow_policy(bad)
        self.assertEqual(str(ctx.exception), "policy field missing")

    def test_wire_json_size_version_is_string_never_number(self):
        bound = signer_mainnet.narrow_policy(_POLICY)
        self.assertIsInstance(bound["size_version"], str)
        self.assertEqual(bound["size_version"], "1")
        payload = signer_mainnet.build_request(
            origin=ORIGIN_MAINNET,
            tree_size=2,
            root=bytes.fromhex(_GOLDEN_ROOT),
            consistency=[],
            timestamp=1700000000,
            request_id="wire-corpus-v1",
            checkpoint=_signed_note(2, bytes.fromhex(_GOLDEN_ROOT)),
            policy=bound,
            token=FIXTURE_TOKEN,
        )
        line = signer_mainnet.encode_request_line(payload)
        raw = line.encode("utf-8")
        self.assertIn(b'"size_version":"1"', raw)
        self.assertNotIn(b'"size_version":1', raw)
        self.assertNotIn(b'"size_version": 1', raw)
        wire = json.loads(line)
        self.assertIsInstance(wire["policy"]["size_version"], str)
        self.assertEqual(wire["policy"]["size_version"], "1")
        self.assertNotIsInstance(wire["policy"]["size_version"], int)

    def test_hmac_preimage_matches_380_byte_golden(self):
        pinned = (CORPUS / "hmac_canonical.txt").read_bytes()
        self.assertEqual(len(pinned), 380)
        self.assertIn(b"size_version=1\n", pinned)
        body = signer_mainnet.canonical_bytes(
            origin=ORIGIN_MAINNET,
            tree_size=2,
            root=_GOLDEN_ROOT,
            consistency=[],
            timestamp=1700000000,
            request_id="golden-v2",
            checkpoint="NOTE",
            policy=_POLICY,
        )
        self.assertEqual(len(body), 380)
        self.assertEqual(body, pinned)
        self.assertIn(b"size_version=1\n", body)
        self.assertTrue(body.startswith(b"pq-anchor/2\n"))
        self.assertTrue(body.endswith(b"v=pq-anchor/2\n"))
        self.assertNotIn(b"policy=", body)

    def test_pinned_request_bytes_match_real_encoder(self):
        bound = signer_mainnet.narrow_policy(_POLICY)
        payload = signer_mainnet.build_request(
            origin=ORIGIN_MAINNET,
            tree_size=2,
            root=bytes.fromhex(_GOLDEN_ROOT),
            consistency=[],
            timestamp=1700000000,
            request_id="wire-corpus-v1",
            checkpoint=_signed_note(2, bytes.fromhex(_GOLDEN_ROOT)),
            policy=bound,
            token=FIXTURE_TOKEN,
        )
        line = signer_mainnet.encode_request_line(payload)
        raw = line.encode("utf-8")
        pinned = (CORPUS / "request.json").read_bytes()
        self.assertEqual(raw, pinned)
        self.assertFalse(pinned.endswith(b"\n"))
        self.assertIn(b'"size_version":"1"', pinned)
        self.assertNotIn(b'"size_version":1,', pinned)
        self.assertNotIn(b'"size_version":1}', pinned)

    def test_pinned_hmac_reject_has_no_authorization_keys(self):
        raw = (CORPUS / "reject.json").read_bytes()
        self.assertEqual(raw, b'{"ok":false,"error":"hmac"}')
        data = json.loads(raw.decode("utf-8"))
        self.assertIs(data["ok"], False)
        self.assertEqual(data["error"], "hmac")
        self.assertTrue(signer_mainnet.hmac_error_expected(data["error"]))
        for key in ("signed", "pqsig", "SignedTxn"):
            self.assertNotIn(key, data)
        self.assertEqual(set(data), {"ok", "error"})


if __name__ == "__main__":
    unittest.main()
