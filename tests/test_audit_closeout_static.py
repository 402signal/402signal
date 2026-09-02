"""Static greps for the audit closeout. No live seller network."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
FLY = (ROOT / "fly.toml").read_text(encoding="utf-8")
REQ = (ROOT / "requirements.txt").read_text(encoding="utf-8")
EVENTS = (ROOT / "live402" / "pq" / "events.py").read_text(encoding="utf-8")
RECEIPT = (ROOT / "live402" / "pq" / "receipt.py").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class CloseoutStaticTests(unittest.TestCase):
    def test_dockerfile_digest_pin_and_no_broadcast(self):
        self.assertIn(
            "python:3.12.14-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc",
            DOCKERFILE,
        )
        self.assertIn("python:3.12.14-slim", DOCKERFILE)
        self.assertIn("gh-150743", DOCKERFILE)
        self.assertNotIn("python:3.12.11-slim@", DOCKERFILE)
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", DOCKERFILE)
        self.assertNotIn("LIVE402_PQ_FALCON_SK", DOCKERFILE)
        self.assertFalse(any(ln.startswith("USER ") for ln in DOCKERFILE.splitlines()))

    def test_cryptography_stays_exact_pin(self):
        self.assertRegex(REQ.strip(), r"^cryptography==50\.0\.1$")
        self.assertNotIn("--require-hashes", DOCKERFILE)

    def test_fly_health_check_unchanged_and_broadcast_unset(self):
        self.assertIn('path = "/health"', FLY)
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", FLY)
        self.assertIn("LIVE402_PQ_FALCON_NETWORK = \"mainnet\"", FLY)
        self.assertIn("LIVE402_PQ_LOG_DB = \"/data/pq-log-mainnet.sqlite\"", FLY)
        self.assertIn("LIVE402_PQ_LOG_EPOCH = \"mainnet-v1\"", FLY)
        self.assertIn("LIVE402_PQ_LOG_ORIGIN = \"402signal.com/pq/log/mainnet-v1\"", FLY)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_BROADCAST", FLY)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_CANARY", FLY)
        # Proposed /ready check is comment-only.
        active = [
            ln for ln in FLY.splitlines()
            if ln.strip() == 'path = "/ready"' and not ln.lstrip().startswith("#")
        ]
        self.assertEqual(active, [])

    def test_v1_v2_event_types_not_mutated(self):
        self.assertIn('TYPE_ROUTE_DECISION = "402signal.route_decision.v1"', EVENTS)
        self.assertIn('TYPE_ROUTE_DECISION_V2 = "402signal.route_decision.v2"', EVENTS)
        self.assertIn("def commitment_hash_v2", EVENTS)
        self.assertIn("def verify_reveal(", EVENTS)
        self.assertIn("def route_decision_event_v2(", EVENTS)
        self.assertIn("V2_PUBLIC_FIELDS", EVENTS)
        self.assertIn("live", EVENTS.split("V2_PUBLIC_FIELDS")[1][:200])

    def test_website_production_mainnet_copy(self):
        home = _read("live402/static/index.html")
        self.assertIn("Algorand MainNet", home)
        self.assertIn("Awaiting checkpoint", home)
        self.assertIn('class="pq-chip"', home)
        self.assertIn("Falcon anchors checkpoints, not merchant payments.", home)
        self.assertIn("PQ Trust is 402Signal's append-only transparency layer", home)
        self.assertIn('class="pq-trust"', home)
        self.assertNotIn("pq-testnet", home)
        self.assertNotIn("Signed checkpoints are periodically anchored to Algorand MainNet", home)
        self.assertNotIn("Currently Algorand TestNet", home)
        self.assertNotIn("periodically anchored to Algorand TestNet", home)
        self.assertNotIn("quantum-proof", home.lower())
        self.assertNotIn("—", home)
        trans = _read("live402/pq/transparency.py")
        self.assertIn("Algorand MainNet log · awaiting first", trans)
        self.assertIn("Production log identity is Algorand MainNet", trans)
        self.assertIn("PQ Trust is 402Signal's append-only transparency layer", trans)
        self.assertNotIn("Currently Algorand TestNet", trans)
        self.assertIn("Awaiting first confirmed", trans)
        self.assertIn("Historical TestNet archive", trans)
        fly = _read("fly.toml")
        self.assertIn("GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU", fly)
        self.assertNotIn("OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU", fly)
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", fly)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_BROADCAST", fly)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_CANARY", fly)

    def test_no_mainnet_falcon_submit(self):
        algo = _read("live402/pq/algo_anchor.py")
        self.assertIn("testnet", algo.lower())
        self.assertIn("MAINNET_BROADCAST_ENV", algo)
        self.assertIn("def automatic_mainnet_enabled", algo)
        self.assertIn("return False", algo.split("def automatic_mainnet_enabled")[1][:400])
        self.assertIn("Never posts MainNet", algo)
        self.assertIn("MAINNET_CANARY_ENV", algo)
        self.assertIn("canary gate off", algo)
        self.assertIn("def _post_mainnet", algo)
        worker = _read("live402/pq/worker.py")
        self.assertIn("Automatic MainNet is off", worker)
        self.assertNotIn("submit_mainnet_canary", worker)
        boot = _read("live402/server.py")
        self.assertNotIn("submit_mainnet_canary", boot)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_CANARY", FLY)
        self.assertNotIn("LIVE402_PQ_FALCON_MAINNET_BROADCAST", FLY)

    def test_no_raw_payment_logging(self):
        route = _read("live402/route.py")
        self.assertIn("settlement_success=", route)
        self.assertNotIn("PAYMENT-SIGNATURE", route.split("def _log_settle")[1][:400])

    def test_authored_docs_have_no_em_dash(self):
        authored = [
            "docs/route-decision-v3.md",
            "docs/settlement-provenance.md",
            "docs/fly-ready-check.md",
            "docs/docker.md",
            "docs/pq-mainnet-prep.md",
            "docs/pq-testnet-archive.md",
            "docs/signer-mainnet-spec.md",
            "docs/pq-key-ceremony.md",
            "docs/pq-funding.md",
            "docs/pq-recovery.md",
            "docs/pq-first-production-event.md",
            "docs/backup.md",
            "docs/automation-security-boundaries.md",
            "docs/github-protection.md",
            "docs/runbooks/mainnet-prelaunch-reset.md",
        ]
        em = "\u2014"
        for rel in authored:
            text = _read(rel)
            self.assertNotIn(em, text, msg=rel)

    def test_signer_spec_requires_durable_security_state(self):
        spec = _read("docs/signer-mainnet-spec.md")
        self.assertIn("durable **security** state", spec)
        self.assertIn("must not authorize X/N/R2", spec)
        self.assertNotIn(
            "The signer is stateless across requests except",
            spec,
        )
        ceremony = _read("docs/pq-key-ceremony.md")
        self.assertIn("algokey pq generate --scheme f1 --keyfile", ceremony)
        self.assertIn("algokey pq info --keyfile", ceremony)
        self.assertIn("TWO separate offline mnemonic backups", ceremony)
        self.assertIn("algokey pq import", ceremony)

    def test_v3_receipt_verify_exists(self):
        self.assertIn("def verify_route_receipt", RECEIPT)
        self.assertIn("TYPE_ROUTE_DECISION_V3", RECEIPT)
        self.assertIn("private_evidence_v3_from_route", RECEIPT)


if __name__ == "__main__":
    unittest.main()
