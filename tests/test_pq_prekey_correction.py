"""Ross FINAL PRE-KEY CORRECTION PASS items 1-6. No live MainNet. No secrets."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN_MAINNET, algo_anchor, canary, mainnet_params, signer_mainnet, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env, insert_authorized_fixture

_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_FALCON_PK = b"pk" + bytes(range(14))
_FALCON_SIG = b"sig" + bytes(range(29))

# Published by 402signal-pq-signer @ 1c3e640ae856a6c7a47cd892d0bfa1794df5deb5.
# Flat k=v, size_version=1, v=pq-anchor/2. Exactly 380 UTF-8 bytes.
_GOLDEN_ROOT = "abababababababababababababababababababababababababababababababab"
_GOLDEN_POLICY = {
    "canonical_fee": 3000,
    "fee_per_byte": 0,
    "fv": 10,
    "last_round": 10,
    "lv": 1010,
    "min_fee": 1000,
    "size_rule": "deterministic_falcon_envelope_estimate",
    "size_version": 1,
    "snapshot_at": 1700000000,
}
FLAT_HMAC_GOLDEN = (
    "pq-anchor/2\n"
    "canonical_fee=3000\n"
    "checkpoint=NOTE\n"
    "consistency=\n"
    "fee_per_byte=0\n"
    "fv=10\n"
    "last_round=10\n"
    "lv=1010\n"
    "min_fee=1000\n"
    "origin=402signal.com/pq/log/mainnet-v1\n"
    "request_id=golden-v2\n"
    "root=abababababababababababababababababababababababababababababababab\n"
    "size_rule=deterministic_falcon_envelope_estimate\n"
    "size_version=1\n"
    "snapshot_at=1700000000\n"
    "timestamp=1700000000\n"
    "tree_size=2\n"
    "v=pq-anchor/2\n"
)
FLAT_HMAC_FIELD_ORDER = (
    "canonical_fee",
    "checkpoint",
    "consistency",
    "fee_per_byte",
    "fv",
    "last_round",
    "lv",
    "min_fee",
    "origin",
    "request_id",
    "root",
    "size_rule",
    "size_version",
    "snapshot_at",
    "timestamp",
    "tree_size",
    "v",
)


def _signed_note(size, root, origin=ORIGIN_MAINNET):
    body = ckpt.checkpoint_body(origin, int(size), bytes(root))
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, origin, _SIG)


def _mainnet_signed(size, root, addr=None, fee=3000, fv=1, lv=1001):
    from live402 import algo_tx

    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN_MAINNET, int(size), bytes(root))
    gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
    txn = algo_tx.pay_txn(addr, addr, 0, fee, fv, lv, algo_anchor.MAINNET_GENESIS_ID, gh, note=note)
    return algo_tx.msgpack_encode(
        {"pqsig": {"pk": _FALCON_PK, "sch": "f1", "sig": _FALCON_SIG, "slt": 0}, "txn": txn}
    )


def _algod_body(last_round=50_000_001, min_fee=1000, fee=0):
    return {
        "last-round": last_round,
        "min-fee": min_fee,
        "fee": fee,
        "genesis-id": algo_anchor.MAINNET_GENESIS_ID,
        "genesis-hash": algo_anchor.MAINNET_GENESIS_HASH,
    }


def _indexer(txid, size, root, addr=None, fee=3000, fv=1, lv=1001, genesis_hash=None):
    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN_MAINNET, int(size), bytes(root))
    txn = {
        "id": txid,
        "confirmed-round": 99,
        "genesis-id": algo_anchor.MAINNET_GENESIS_ID,
        "tx-type": "pay",
        "sender": addr,
        "fee": fee,
        "first-valid": fv,
        "last-valid": lv,
        "note": base64.b64encode(note).decode("ascii"),
        "payment-transaction": {"amount": 0, "receiver": addr},
        "signature": {
            "pqsig": {
                "scheme": "f1",
                "salt": 0,
                "public-key": base64.b64encode(_FALCON_PK).decode("ascii"),
                "signature": base64.b64encode(_FALCON_SIG).decode("ascii"),
            }
        },
    }
    if genesis_hash is not None:
        txn["genesis-hash"] = genesis_hash
    return {"transaction": txn}


class PrekeyCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        store.reset()
        store.append(b"canary-leaf")
        self.root = store.root(1)
        store.save_checkpoint(1, _signed_note(1, self.root))
        self.params = {
            "minFee": 1000,
            "fee": 0,
            "lastRound": 1,
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        self.blob = _mainnet_signed(1, self.root)
        self.expected = algo_anchor.signed_txn_txid(self.blob)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_fixture_mode_never_dials_params(self):
        with patch("urllib.request.build_opener", side_effect=AssertionError("must not dial")):
            with self.assertRaises(mainnet_params.ParamsError):
                mainnet_params.fetch_trusted_mainnet_params()

    def test_params_parse_and_reject_caller_url(self):
        parsed = mainnet_params.parse_algod_params(_algod_body(last_round=42, fee=7))
        self.assertEqual(parsed["lastRound"], 42)
        self.assertEqual(parsed["minFee"], 1000)
        self.assertEqual(parsed["feePerByte"], 7)
        self.assertTrue(parsed["require_canonical"])
        with self.assertRaises(mainnet_params.ParamsError):
            mainnet_params.params_url_for("evil.example")
        with self.assertRaises(mainnet_params.ParamsError):
            mainnet_params.parse_algod_params(
                {
                    "last-round": 1,
                    "min-fee": 1000,
                    "fee": 0,
                    "genesis-id": "testnet-v1.0",
                    "genesis-hash": algo_anchor.MAINNET_GENESIS_HASH,
                }
            )

    def test_authorize_fetches_trusted_snapshot(self):
        fetched = []

        def fetch():
            fetched.append(1)
            return self.params

        row = canary.authorize(fetch_params_fn=fetch, sign_fn=lambda _i: self.blob)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(canary.send_state_of(row), canary.STATE_AUTHORIZED)
        policy = json.loads(row["fee_policy"])
        self.assertEqual(policy["last_round"], 1)
        self.assertEqual(policy["canonical_fee"], 3000)
        self.assertEqual(policy["fv"], 1)
        self.assertEqual(policy["lv"], 1001)
        self.assertEqual(policy["size_rule"], "deterministic_falcon_envelope_estimate")
        self.assertEqual(policy["size_version"], 1)

    def test_empty_params_do_not_hide_missing_snapshot(self):
        with self.assertRaises(canary.CanaryError):
            canary.authorize(params={}, sign_fn=lambda _i: self.blob)

    def test_observation_times_agree_or_fail_closed(self):
        policy = algo_anchor.hmac_policy(
            algo_anchor.fee_policy_snapshot(self.params, now=1_700_000_000)
        )
        near = dict(self.params)
        near["lastRound"] = 5
        agreed = algo_anchor.validate_observed_against_router_policy(
            policy, near, now=1_700_000_000
        )
        self.assertEqual(agreed["canonical_fee"], 3000)
        far = dict(self.params)
        far["lastRound"] = 10_000
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_observed_against_router_policy(policy, far, now=1_700_000_000)
        congested = {"minFee": 1000, "fee": 50, "lastRound": 1, "genesisID": algo_anchor.MAINNET_GENESIS_ID}
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_observed_against_router_policy(
                policy, congested, now=1_700_000_000
            )
        with self.assertRaises(canary.CanaryError):
            canary.authorize(
                params=self.params,
                observed_params=congested,
                sign_fn=lambda _i: (_ for _ in ()).throw(AssertionError("must not sign")),
            )
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))

    def test_pq_anchor_2_hmac_flattens_policy(self):
        policy = {
            "last_round": 9,
            "min_fee": 1000,
            "fee_per_byte": 0,
            "fv": 9,
            "lv": 1009,
            "canonical_fee": 3000,
            "snapshot_at": 1_700_000_000,
            "size_rule": "deterministic_falcon_envelope_estimate",
            "size_version": 1,
        }
        body = signer_mainnet.canonical_bytes(
            origin=ORIGIN_MAINNET,
            tree_size=1,
            root=b"\x11" * 32,
            consistency=[],
            timestamp=1_700_000_000,
            request_id="req",
            checkpoint=_signed_note(1, b"\x11" * 32),
            policy=policy,
        )
        self.assertTrue(body.startswith(b"pq-anchor/2\n"))
        self.assertIn(b"canonical_fee=3000\n", body)
        self.assertIn(b"size_version=1\n", body)
        self.assertIn(b"size_rule=deterministic_falcon_envelope_estimate\n", body)
        self.assertIn(b"v=pq-anchor/2\n", body)
        self.assertNotIn(b"policy=", body)
        self.assertNotIn(b"txn=", body)
        missing = {**policy, "size_version": 2}
        with self.assertRaises(signer_mainnet.SignerClientError):
            signer_mainnet.canonical_bytes(
                origin=ORIGIN_MAINNET,
                tree_size=1,
                root=b"\x11" * 32,
                consistency=[],
                timestamp=1_700_000_000,
                request_id="req",
                checkpoint=_signed_note(1, b"\x11" * 32),
                policy=missing,
            )

    def test_summary_is_read_only_and_does_not_block_later_prepare(self):
        signed = []
        info = canary.inspect(
            fetch_params_fn=lambda: self.params,
            router_sha="deadbeef",
        )
        self.assertTrue(info["read_only"])
        self.assertIsNone(info["authorized"])
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))
        later = 1_700_000_000 + 91
        row = canary.prepare(
            now=later,
            fetch_params_fn=lambda: self.params,
            sign_fn=lambda _i: signed.append(1) or self.blob,
        )
        self.assertEqual(len(signed), 1)
        self.assertEqual(row["state"], canary.STATE_AUTHORIZED)

    def test_cli_summary_does_not_authorize(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "pq_mainnet_canary.py"
        spec = importlib.util.spec_from_file_location("pq_mainnet_canary_ro", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with patch.object(canary, "authorize", side_effect=AssertionError("summary must not authorize")):
            rc = mod.main(["--summary-only"])
        self.assertEqual(rc, 0)
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))
        rc_prepare = mod.main(["--prepare"])
        self.assertEqual(rc_prepare, 2)

    def test_go_does_not_create_fresh_auth(self):
        with self.assertRaises(canary.CanaryError):
            canary.send_persisted(authorize_human_canary=True)

    def test_stale_authorized_requires_explicit_discard(self):
        row = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        policy = json.loads(row["fee_policy"])
        policy["snapshot_at"] = 1
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            request_id=row["request_id"],
            signed=row["signed"],
            at=row["at"],
            send_state=canary.STATE_AUTHORIZED,
            fee_policy=policy,
            fv=row["fv"],
            lv=row["lv"],
        )
        with self.assertRaises(canary.CanaryError) as ctx:
            canary.authorize(params=self.params, sign_fn=lambda _i: self.blob, now=int(__import__("time").time()))
        self.assertIn("discard", str(ctx.exception).lower())
        canary.discard_authorized()
        again = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        self.assertEqual(canary.send_state_of(again), canary.STATE_AUTHORIZED)

    def test_cannot_discard_after_send_attempted(self):
        row = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        insert_authorized_fixture(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            signed=row["signed"],
            send_state=canary.STATE_SEND_ATTEMPTED,
            expected_txid=self.expected,
            fee_policy=row["fee_policy"],
            fv=row["fv"],
            lv=row["lv"],
        )
        with self.assertRaises(canary.CanaryError):
            canary.discard_authorized()

    def test_confirm_rejects_mutated_fee_fv_lv(self):
        row = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            request_id=row["request_id"],
            signed=row["signed"],
            at=row["at"],
            send_state=canary.STATE_SEND_ATTEMPTED,
            expected_txid=self.expected,
            fee_policy=row["fee_policy"],
            fv=row["fv"],
            lv=row["lv"],
            send_attempted_at=1,
        )
        stored = store.authorized_at(1)
        for mutated in (
            _indexer(self.expected, 1, self.root, fee=4000, fv=1, lv=1001),
            _indexer(self.expected, 1, self.root, fee=3000, fv=2, lv=1001),
            _indexer(self.expected, 1, self.root, fee=3000, fv=1, lv=1002),
        ):
            with self.assertRaises((canary.CanaryError, algo_anchor.AnchorError)):
                canary.poll_expected(stored, fetch_fn=lambda _t, m=mutated: m)

    def test_decode_preserves_fv_lv_genesis_hash(self):
        decoded = algo_anchor.decode_chain_txn(
            _indexer(self.expected, 1, self.root, genesis_hash=algo_anchor.MAINNET_GENESIS_HASH)
        )
        self.assertEqual(decoded["fee"], 3000)
        self.assertEqual(decoded["fv"], 1)
        self.assertEqual(decoded["lv"], 1001)
        self.assertEqual(decoded["genesis_id"], algo_anchor.MAINNET_GENESIS_ID)
        self.assertEqual(decoded["genesis_hash"], algo_anchor.MAINNET_GENESIS_HASH)

    def test_store_monotonicity_fail_closed(self):
        row = canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)
        latched = store.save_authorized_checkpoint(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            request_id=row["request_id"],
            signed=row["signed"],
            at=row["at"],
            send_state=canary.STATE_SEND_ATTEMPTED,
            expected_txid=self.expected,
            fee_policy=row["fee_policy"],
            fv=row["fv"],
            lv=row["lv"],
        )
        self.assertEqual(latched["send_state"], canary.STATE_SEND_ATTEMPTED)
        with self.assertRaises(store.StoreError):
            store.save_authorized_checkpoint(
                tree_size=1,
                origin=row["origin"],
                root=row["root"],
                checkpoint=row["checkpoint"],
                request_id=row["request_id"],
                signed=row["signed"],
                at=row["at"],
                send_state=canary.STATE_AUTHORIZED,
                expected_txid=self.expected,
                fee_policy=row["fee_policy"],
                fv=row["fv"],
                lv=row["lv"],
            )
        submitted = store.save_authorized_checkpoint(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            request_id=row["request_id"],
            signed=row["signed"],
            at=row["at"],
            submitted=True,
            txid=self.expected,
            send_state=canary.STATE_SUBMITTED,
            expected_txid=self.expected,
            fee_policy=row["fee_policy"],
            fv=row["fv"],
            lv=row["lv"],
        )
        self.assertEqual(submitted["send_state"], canary.STATE_SUBMITTED)
        with self.assertRaises(store.StoreError):
            store.save_authorized_checkpoint(
                tree_size=1,
                origin=row["origin"],
                root=row["root"],
                checkpoint=row["checkpoint"],
                request_id=row["request_id"],
                signed=row["signed"],
                at=row["at"],
                send_state=canary.STATE_SEND_ATTEMPTED,
                expected_txid=self.expected,
                fee_policy=row["fee_policy"],
                fv=row["fv"],
                lv=row["lv"],
            )
        with self.assertRaises(store.StoreError):
            store.save_authorized_checkpoint(
                tree_size=1,
                origin=row["origin"],
                root=row["root"],
                checkpoint=row["checkpoint"],
                request_id=row["request_id"],
                signed=self.blob + b"\x00",
                at=row["at"],
                send_state=canary.STATE_SUBMITTED,
                expected_txid=self.expected,
                fee_policy=row["fee_policy"],
                fv=row["fv"],
                lv=row["lv"],
            )

    def test_preflight_not_probed_is_not_healthy(self):
        from live402.pq import monitor

        health = monitor.preflight()
        self.assertFalse(health["signer"].get("probed"))
        self.assertFalse(health["signer"].get("available"))
        self.assertFalse(health["confirm_provider"].get("probed"))
        self.assertFalse(health["confirm_provider"].get("reachable"))

    def test_falcon_compatible_stays_false(self):
        from live402.pq import network as netcfg

        self.assertFalse(netcfg.CONFIRM_FALCON_COMPATIBLE["tatum"])
        self.assertFalse(netcfg.CONFIRM_FALCON_COMPATIBLE["nownodes"])
        status = netcfg.confirmation_status("mainnet")
        self.assertFalse(status["confirm_falcon_compatible"])
        self.assertFalse(status["confirmation_ready"])

    def test_flat_hmac_golden_vector_matches_go_signer_format(self):
        self.assertEqual(signer_mainnet.CANONICAL_KEYS, FLAT_HMAC_FIELD_ORDER)
        self.assertEqual(signer_mainnet.HMAC_SIZE_VERSION, 1)
        want = FLAT_HMAC_GOLDEN.encode("utf-8")
        self.assertEqual(len(want), 380)
        body = signer_mainnet.canonical_bytes(
            origin=ORIGIN_MAINNET,
            tree_size=2,
            root=_GOLDEN_ROOT,
            consistency=[],
            timestamp=1700000000,
            request_id="golden-v2",
            checkpoint="NOTE",
            policy=_GOLDEN_POLICY,
        )
        self.assertEqual(len(body), 380)
        self.assertEqual(body, want)
        self.assertNotIn(b"policy=", body)
        self.assertTrue(body.startswith(b"pq-anchor/2\n"))
        self.assertTrue(body.endswith(b"v=pq-anchor/2\n"))
        spec = (__import__("pathlib").Path("docs/signer-mainnet-spec.md").read_text(encoding="utf-8"))
        self.assertIn("size_version=1", spec)
        self.assertIn("v=pq-anchor/2", spec)
        self.assertNotIn("policy=<canonical_fee=", spec)

    def test_ready_stays_no(self):
        text = (__import__("pathlib").Path("docs/pq-prekey-closeout.md").read_text(encoding="utf-8"))
        self.assertIn("READY_FOR_PRODUCTION_KEY_INSTALL = NO", text)
        spec = (__import__("pathlib").Path("docs/signer-mainnet-spec.md").read_text(encoding="utf-8"))
        self.assertIn("pq-anchor/2", spec)
        self.assertIn("Parallel private-signer PR checklist", spec)
        self.assertIn("size_version", spec)


if __name__ == "__main__":
    unittest.main()
