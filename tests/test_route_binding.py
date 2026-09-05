"""Adversarial v4 tests. No live payments, signing accounts or network access."""

import base64
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_success_only_billing import (
    RESOURCE,
    TYPED_MISSES,
    _headers,
    _miss,
    _payload,
    _settled,
    _verified,
    _winner,
)

from live402 import payment, probe, replay, route
from live402 import route_binding as rb
from live402.pq import events, receipt, route_v4, store


def bound_winner(rail="base", now=None, method="GET", request_body=b""):
    result = _winner(rail)
    now = int(time.time()) if now is None else now
    result["probed_at"] = events.jcs.utc_seconds_z(now)
    result["binding_observation"] = {
        "request": rb.request_context(result["url"], method, request_body),
        "observed_at": now,
        "quote_sha256": rb.digest(result["envelope"]),
    }
    return result


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "LIVE402_FIXTURE": "1",
                "LOCAL_FREE": "0",
                "LIVE402_PQ_LOG": "1",
                "LIVE402_PQ_LOG_DB": self.tmp.name + "/log.sqlite",
                "LIVE402_REPLAY_DB": self.tmp.name + "/replay.sqlite",
                "LIVE402_ROUTE_BINDING_TTL_S": "60",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        store.reset()
        replay.reset()
        self.key = Ed25519PrivateKey.generate()
        self.vkey = receipt.configure_signer(self.key)
        self.addCleanup(self.cleanup)
        self.body = {"need": "weather", "require_route_binding": True}

    def cleanup(self):
        receipt.configure_signer(None)
        store.reset()
        replay.reset()

    def issue(self, rail="base", now=None):
        result = bound_winner(rail, now)
        result["decision_binding"] = rb.build(result, self.body, now=now)
        return receipt.attach_to_route(result, self.body)

    def check(self, result, **changes):
        args = {
            "vkey": self.vkey,
            "status": 402,
            "envelope": result["envelope"],
            "url": result["url"],
            "method": "GET",
        }
        args.update(changes)
        return rb.verify_route(result, self.body, **args)

    def test_all_rails_signed_roundtrip_and_private_public_boundary(self):
        for rail in ("base", "solana", "algorand"):
            result = self.issue(rail)
            self.assertEqual(self.check(result), result["envelope"]["accepts"][0])
            tr = result["pq_trust"]["transparency"]
            self.assertEqual(tr["leaf_type"], route_v4.TYPE)
            public = json.loads(store.leaf_at(tr["index"])["body"])
            self.assertEqual(set(public), {"type", "ts", "nonce", "commitment"})
            self.assertNotIn("seller.example", json.dumps(public))
            self.assertNotIn(result["payTo"], json.dumps(public))
            self.assertNotIn("weather", json.dumps(public))

    def test_every_quote_field_and_resource_mutation_blocks(self):
        result = self.issue()
        for key, value in {
            "amount": "99999",
            "payTo": "0x" + "11" * 20,
            "network": payment.SOLANA_MAINNET,
            "asset": "USDT",
            "scheme": "upto",
            "maxTimeoutSeconds": 600,
            "extra": {"facilitator": "https://other.example"},
        }.items():
            env = copy.deepcopy(result["envelope"])
            env["accepts"][0][key] = value
            with self.subTest(key=key), self.assertRaises(rb.BindingError):
                self.check(result, envelope=env)
        for args in (
            {"url": result["url"] + "?other=1"},
            {"method": "POST"},
            {"status": 200},
            {"body": b"changed"},
        ):
            with self.subTest(args=args), self.assertRaises(rb.BindingError):
                self.check(result, **args)

    def test_full_envelope_changes_and_unknown_extensions_block(self):
        result = self.issue()
        for change in (
            {"error": "new"},
            {"extensions": {"new-spending-mode": {}}},
            {"resource": {"url": "https://other.example"}},
            {"x402Version": 1},
            {"accepts": result["envelope"]["accepts"] * 2},
        ):
            with self.subTest(change=change), self.assertRaises(rb.BindingError):
                self.check(result, envelope={**result["envelope"], **change})

    def test_expiration_bound_to_observation_no_reissue_extension(self):
        result = self.issue(now=1000)
        self.check(result, now=1059)
        for now in (999, 1060, 2000):
            with self.assertRaises(rb.BindingError):
                self.check(result, now=now)
        with self.assertRaises(rb.BindingError):
            rb.build(bound_winner(now=1000), self.body, now=1060)

    def test_tampered_evidence_receipt_and_wrong_key_rejected(self):
        result = self.issue()
        for section, field, value in (
            ("binding", "expires_at", result["decision_binding"]["expires_at"] + 10),
            ("evidence", "request_json", '{"need":"different"}'),
            ("evidence", "routing_evidence_json", "{}"),
            ("reveal", "salt", "00" * 32),
            ("receipt", "index", True),
            ("receipt", "leaf_hash", "00" * 32),
        ):
            bad = copy.deepcopy(result)
            tr = bad["pq_trust"]["transparency"]
            obj = {
                "binding": tr["reveal"]["evidence"]["binding"],
                "evidence": tr["reveal"]["evidence"],
                "reveal": tr["reveal"],
                "receipt": tr["receipt"],
            }[section]
            obj[field] = value
            with (
                self.subTest(section=section, field=field),
                self.assertRaises(rb.BindingError),
            ):
                self.check(bad)
        wrong = receipt.configure_signer(Ed25519PrivateKey.generate())
        for key in (wrong, ""):
            with self.assertRaises(rb.BindingError):
                self.check(result, vkey=key)
        with self.assertRaises(rb.BindingError):
            rb.verify_route(
                result,
                {**self.body, "need": "other"},
                vkey=self.vkey,
                status=402,
                envelope=result["envelope"],
                url=result["url"],
                method="GET",
            )

    def test_ambiguous_json_duplicate_keys_and_header_body_differentials(self):
        raw = json.dumps(_winner()["envelope"]).encode()
        self.assertEqual(rb.observed_challenge(402, {}, raw), _winner()["envelope"])
        header = {"payment-required": base64.b64encode(raw).decode()}
        self.assertEqual(rb.observed_challenge(402, header, raw), _winner()["envelope"])
        for bad in (
            b'{"x402Version":2,"x402Version":1}',
            b'{"x":NaN}',
            b'{"x":9007199254740992}',
            b'{"x":1e100}',
            b"\xff",
            b'{"x":"\\ud800"}',
            b"[" * 1000,
        ):
            with self.subTest(raw=bad[:30]), self.assertRaises(rb.BindingError):
                rb.observed_challenge(402, header, bad)
        with self.assertRaises(rb.BindingError):
            rb.observed_challenge(402, header, raw.replace(b"10000", b"20000"))
        with self.assertRaises(rb.BindingError):
            rb.observed_challenge(402, {"payment-required": ""}, raw)
        with self.assertRaises(rb.BindingError):
            rb.strict_json('"' + "\u00e9" * (rb.MAX_JSON_BYTES // 2) + '"')

    def test_guard_binds_actual_post_body(self):
        result = bound_winner(method="POST", request_body=b"{}")
        binding = rb.build(result, self.body)
        rb.verify_challenge(
            binding,
            status=402,
            envelope=result["envelope"],
            url=result["url"],
            method="POST",
            body=b"{}",
        )
        with self.assertRaises(rb.BindingError):
            rb.verify_challenge(
                binding,
                status=402,
                envelope=result["envelope"],
                url=result["url"],
                method="POST",
                body=b'{"query":"different"}',
            )

    def test_catalog_or_missing_observation_cannot_supply_binding(self):
        for result in (
            _winner(),
            {**bound_winner(), "unresolved_constraints": ["trustworthy"]},
        ):
            with self.assertRaises(rb.BindingError):
                rb.build(result, self.body)
        result = bound_winner()
        result["envelope"]["accepts"] *= 2
        result["binding_observation"]["quote_sha256"] = rb.digest(result["envelope"])
        with self.assertRaises(rb.BindingError):
            rb.build(result, self.body)

    def execute(self, output):
        with (
            patch("live402.route.run_probe", return_value=output) as run,
            patch("live402.facilitator.verify", return_value=_verified()) as verify,
            patch("live402.facilitator.settle", return_value=_settled()) as settle,
            patch("live402.history.mark_batch_settled") as mark,
        ):
            out = route.handle_route(self.body, _headers(_payload()), RESOURCE)
            return out, (
                run.call_count,
                verify.call_count,
                settle.call_count,
                mark.call_count,
            )

    def test_paid_winner_settles_once_and_replay_retains_same_expiry(self):
        out, calls = self.execute((200, bound_winner()))
        self.assertEqual(out[0], 200)
        self.assertEqual(calls, (1, 1, 1, 1))
        self.assertTrue(out[1]["billing"]["settled"])
        self.check(out[1])
        replay.reset_memory()
        again, calls = self.execute((200, bound_winner()))
        self.assertEqual(again, out)
        self.assertEqual(calls, (0, 0, 0, 0))

    def test_unprovable_binding_free_and_terminal_across_restart(self):
        out, calls = self.execute((200, _winner()))
        self.assertEqual(out[0], 503)
        self.assertEqual(calls, (1, 1, 0, 0))
        self.assertFalse(out[1]["billing"]["settled"])
        self.assertNotIn("pq_trust", out[1])
        replay.reset_memory()
        again, calls = self.execute((200, bound_winner()))
        self.assertEqual(again, out)
        self.assertEqual(calls, (0, 0, 0, 0))

    def test_typed_misses_create_no_v4_evidence_or_charge(self):
        for i, reason in enumerate(TYPED_MISSES):
            with (
                patch("live402.route.run_probe", return_value=(503, _miss(reason))),
                patch("live402.facilitator.verify", return_value=_verified()),
                patch("live402.facilitator.settle") as settle,
                patch("live402.pq.receipt.attach_to_route") as attach,
            ):
                out = route.handle_route(
                    self.body, _headers(_payload(str(i))), RESOURCE
                )
                self.assertEqual(out[0], 503)
                self.assertFalse(out[1]["billing"]["settled"])
                settle.assert_not_called()
                attach.assert_not_called()

    def test_failure_after_durable_append_never_appends_twice_or_claims_free(self):
        with (
            patch("live402.pq.receipt.store.ready_to_checkpoint", return_value=False),
            patch(
                "live402.pq.receipt.append_event", wraps=receipt.append_event
            ) as append,
        ):
            out, calls = self.execute((200, bound_winner()))
        self.assertEqual(out[0], 503)
        self.assertTrue(out[1]["billing"]["settled"])
        self.assertEqual(calls, (1, 1, 1, 1))
        self.assertEqual(append.call_count, 1)
        again, calls = self.execute((200, bound_winner()))
        self.assertEqual(again, out)
        self.assertEqual(calls, (0, 0, 0, 0))

    def test_actual_probe_observation_uses_wire_and_method(self):
        result = _winner()
        raw = json.dumps(result["envelope"]).encode()

        def error(url):
            return urllib.error.HTTPError(
                url, 402, "Payment Required", {}, BytesIO(raw)
            )

        opener = MagicMock()
        opener.open.side_effect = error(result["url"])
        with patch("live402.probe._opener", return_value=opener):
            snap = probe._one_request(result["url"], "GET", pinned_addrs=[("fixture",)])
        self.assertEqual(snap["binding_observation"]["request"]["method"], "GET")
        self.assertEqual(
            snap["binding_observation"]["quote_sha256"], rb.digest(result["envelope"])
        )
        opener.open.side_effect = error("https://redirected.example")
        with patch("live402.probe._opener", return_value=opener):
            snap = probe._one_request(result["url"], "GET", pinned_addrs=[("fixture",)])
        self.assertIsNone(snap["binding_observation"])

    def test_invalid_opt_in_is_body_error_after_verify(self):
        self.body["require_route_binding"] = "true"
        with (
            patch("live402.facilitator.verify", return_value=_verified()) as verify,
            patch("live402.facilitator.settle") as settle,
            patch("live402.probe.route_need") as run,
        ):
            out = route.handle_route(self.body, _headers(_payload()), RESOURCE)
            self.assertEqual(out[0], 400)
            verify.assert_called_once()
            run.assert_not_called()
            settle.assert_not_called()

    def test_policy_equality_matches_json_numbers_without_boolean_coercion(self):
        self.body["max_price_usd"] = 1
        result = self.issue()
        self.body["max_price_usd"] = 1.0
        self.check(result)
        self.body["max_price_usd"] = True
        with self.assertRaises(rb.BindingError):
            self.check(result)

    def test_concurrent_unprovable_binding_does_not_duplicate_work(self):
        started, release = threading.Event(), threading.Event()
        results = []

        def unprovable(*_args, **_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return 200, _winner()

        with (
            patch("live402.route.run_probe", side_effect=unprovable) as run,
            patch("live402.facilitator.verify", return_value=_verified()) as verify,
            patch("live402.facilitator.settle") as settle,
            patch("live402.pq.receipt.attach_to_route") as attach,
        ):
            workers = [
                threading.Thread(
                    target=lambda: results.append(
                        route.handle_route(self.body, _headers(_payload()), RESOURCE)
                    )
                )
                for _ in range(2)
            ]
            workers[0].start()
            self.assertTrue(started.wait(timeout=3))
            workers[1].start()
            release.set()
            for worker in workers:
                worker.join(timeout=3)
            run.assert_called_once()
            verify.assert_called_once()
            settle.assert_not_called()
            attach.assert_not_called()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 503)
        self.assertFalse(results[0][1]["billing"]["settled"])

    def test_shared_conformance_vectors_verify_with_pinned_test_key(self):
        from pathlib import Path

        vectors = json.loads(
            (Path(__file__).parent / "fixtures/route-binding-v1.json").read_text()
        )
        for case in vectors["cases"] + vectors["historical_inclusions"]:
            accepted = rb.verify_route(
                case["response"],
                case["request"],
                vkey=vectors["trusted_vkey"],
                status=402,
                envelope=case["challenge"],
                url=case["response"]["url"],
                method=case["method"],
                body=case["body"].encode(),
                now=case["now"],
            )
            self.assertEqual(accepted, case["challenge"]["accepts"][0])
