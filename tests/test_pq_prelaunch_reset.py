"""Ross-only MainNet pre-launch reset helpers. No live Fly. No secrets printed."""

from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from live402.pq import ORIGIN, ORIGIN_MAINNET, checkpoint as ckpt, log_identity, store, trust
from tests.pq_test_env import clear_pq_env

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "scripts" / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vkey(origin: str, key: Ed25519PrivateKey) -> str:
    pk = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return ckpt.vkey_encode(origin, pk)


class RunbookStaticTests(unittest.TestCase):
    def test_runbook_covers_required_steps(self):
        text = (ROOT / "docs/runbooks/mainnet-prelaunch-reset.md").read_text(encoding="utf-8")
        for needle in (
            "Ross only",
            "A. Confirm broadcast",
            "B. Optional offline archive",
            "C. Retire the old MainNet test DB",
            "D. Init a fresh empty",
            "E. Generate a fresh Ed25519",
            "F. Derive the C2SP vkey",
            "G. Install router SK",
            "H. NEVER put the Ed25519 SK on the Falcon signer",
            "I. Verify the new vkey is distinct",
            "J. Verify fresh state is empty",
            "K. No Algorand transaction during reset",
            "L. No copy of old leaves",
            "M. Remove the compromised",
            "TestNet runtime secrets",
            "LIVE402_PQ_LOG_SK",
            "LIVE402_PQ_SIGNER_TOKEN",
            "LIVE402_PQ_FALCON_BROADCAST",
            "MAINNET_FALCON_IDENTITY_DISTINCT",
            "algokey pq info",
            "LIVE402_PQ_LOG_SK_MAINNET=-",
            "LIVE402_PQ_LOG_VKEY_MAINNET=-",
            "umask 077",
            "402signal.com/pq/log/mainnet-v1",
            "/data/pq-log-mainnet.sqlite",
            "leaves=0",
            "Do not generate keys",
        ):
            self.assertIn(needle, text, needle)
        self.assertNotIn("\u2014", text)
        self.assertIn("fly secrets set LIVE402_PQ_LOG_SK_MAINNET=-", text)
        self.assertNotIn("fly secrets set LIVE402_PQ_LOG_SK_MAINNET=<", text)
        self.assertIn("No CLI secret arguments", text)
        self.assertIn("Do not run `scripts/pq_mainnet_canary.py --prepare`", text)
        self.assertIn("Do not decode or `algokey pq info` the Falcon keyfile", text)

    def test_runbook_forbids_cli_secret_and_keyfile_decode(self):
        text = (ROOT / "docs/runbooks/mainnet-prelaunch-reset.md").read_text(encoding="utf-8")
        self.assertIn("No CLI secret arguments", text)
        self.assertIn("Never NAME=hex on the CLI", text)
        self.assertIn("Do not `fly ssh console` and print env", text)
        self.assertIn("Signer does not have `LIVE402_PQ_LOG_SK`", text)


class IdentityHelperTests(unittest.TestCase):
    def test_falcon_address_and_vkey_digest_checks(self):
        test_addr = log_identity.TESTNET_FALCON_PUBLIC_ADDRESS
        main_addr = "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU"
        log_identity.reject_reused_falcon_address(test_addr, main_addr)
        with self.assertRaises(log_identity.ConfigError):
            log_identity.reject_reused_falcon_address(test_addr, test_addr)
        a = Ed25519PrivateKey.generate()
        b = Ed25519PrivateKey.generate()
        test_v = _vkey(ORIGIN, a)
        main_v = _vkey(ORIGIN_MAINNET, b)
        reused = _vkey(ORIGIN_MAINNET, a)
        log_identity.reject_reused_ed25519_vkey(test_v, main_v)
        with self.assertRaises(log_identity.ConfigError):
            log_identity.reject_reused_ed25519_vkey(test_v, reused)
        digest = log_identity.public_string_digest(reused)
        log_identity.reject_compromised_ed25519_vkey(main_v, digest)
        with self.assertRaises(log_identity.ConfigError):
            log_identity.reject_compromised_ed25519_vkey(reused, digest)
        self.assertNotEqual(log_identity.public_string_digest(test_addr), log_identity.public_string_digest(main_addr))

    def test_require_mainnet_identity_rejects_testnet_falcon_address(self):
        tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        db = os.path.join(tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = log_identity.TESTNET_FALCON_PUBLIC_ADDRESS
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        try:
            with self.assertRaises(log_identity.ConfigError) as ctx:
                log_identity.require_mainnet_identity(db_path=db, origin=ORIGIN_MAINNET)
            self.assertIn("reuses testnet address", str(ctx.exception))
        finally:
            clear_pq_env()
            tmp.cleanup()


class PublicDescriptorMainNetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_mainnet_trust_uses_mainnet_vkey_env_not_testnet(self):
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = (
            "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU"
        )
        a = Ed25519PrivateKey.generate()
        b = Ed25519PrivateKey.generate()
        test_v = _vkey(ORIGIN, a)
        main_v = _vkey(ORIGIN_MAINNET, b)
        os.environ["LIVE402_PQ_LOG_VKEY"] = test_v
        os.environ["LIVE402_PQ_LOG_VKEY_MAINNET"] = main_v
        self.assertEqual(trust.vkey(), main_v)
        self.assertNotEqual(trust.vkey(), test_v)
        desc = trust.public_descriptor()
        self.assertEqual(desc["origin"], ORIGIN_MAINNET)
        self.assertEqual(desc["epoch"], "mainnet-v1")
        self.assertEqual(desc["falcon"]["network"], "mainnet-v1.0")
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "none")
        self.assertTrue(desc["not_mainnet_go"])
        self.assertEqual(desc["log_signature"]["vkey"], main_v)
        self.assertNotIn("sk", desc["log_signature"])
        self.assertNotIn(test_v, str(desc))
        blob = str(desc).lower()
        self.assertNotIn("begin private key", blob)

    def test_testnet_public_descriptor_unchanged_without_mainnet_env(self):
        desc = trust.public_descriptor()
        self.assertEqual(desc["falcon"]["network"], "testnet-v1.0")
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "testnet")


class DeriveVkeyScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = _load("pq_derive_vkey")
        self.seed = os.urandom(32)
        self.hex_sk = self.seed.hex()
        self.sk_path = Path(self.tmp.name) / "sk.hex"
        self.sk_path.write_text(self.hex_sk + "\n", encoding="utf-8")
        os.chmod(self.sk_path, 0o600)
        self.addCleanup(self.tmp.cleanup)

    def test_derive_from_file_prints_vkey_not_sk(self):
        vkey_out = Path(self.tmp.name) / "vkey.txt"
        digest_out = Path(self.tmp.name) / "vkey.sha256"
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            rc = self.mod.main(
                [
                    "--origin",
                    ORIGIN_MAINNET,
                    "--sk-file",
                    str(self.sk_path),
                    "--vkey-out",
                    str(vkey_out),
                    "--digest-out",
                    str(digest_out),
                ]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        err_s = err.getvalue()
        self.assertNotIn(self.hex_sk, out)
        self.assertNotIn(self.hex_sk, err_s)
        self.assertNotIn(self.hex_sk, vkey_out.read_text(encoding="utf-8"))
        vkey = out.strip()
        parsed = ckpt.vkey_parse(vkey)
        self.assertEqual(parsed["name"], ORIGIN_MAINNET)
        self.assertEqual(vkey_out.read_text(encoding="utf-8").strip(), vkey)
        self.assertEqual(
            digest_out.read_text(encoding="utf-8").strip(),
            log_identity.public_string_digest(vkey),
        )

    def test_refuses_secret_cli_arg_and_tty_stdin(self):
        self.mod.refuse_secret_cli_args(["--sk-file", str(self.sk_path)])
        with self.assertRaises(SystemExit):
            self.mod.refuse_secret_cli_args([self.hex_sk])
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            rc = self.mod.main([self.hex_sk])
        self.assertEqual(rc, 2)
        self.assertIn("secret-shaped", err.getvalue())
        self.assertNotIn(self.hex_sk, buf.getvalue())
        buf = io.StringIO()
        with patch("sys.stdin") as stdin, patch("sys.stdout", buf), patch("sys.stderr", io.StringIO()):
            stdin.isatty.return_value = True
            rc = self.mod.main(["--origin", ORIGIN_MAINNET])
        self.assertEqual(rc, 2)
        self.assertNotIn(self.hex_sk, buf.getvalue())

    def test_digest_only_omits_vkey_string(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", io.StringIO()):
            rc = self.mod.main(
                ["--origin", ORIGIN_MAINNET, "--sk-file", str(self.sk_path), "--digest-only"]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue().strip()
        self.assertEqual(len(out), 64)
        self.assertNotIn("+", out)
        self.assertNotIn(self.hex_sk, out)


class PublicIdentityScriptTests(unittest.TestCase):
    def test_distinct_addresses_and_vkeys(self):
        mod = _load("pq_public_identity_check")
        a = Ed25519PrivateKey.generate()
        b = Ed25519PrivateKey.generate()
        test_v = _vkey(ORIGIN, a)
        main_v = _vkey(ORIGIN_MAINNET, b)
        compromised = _vkey(ORIGIN_MAINNET, a)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = mod.main(
                [
                    "--mainnet-address",
                    "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU",
                    "--testnet-address",
                    log_identity.TESTNET_FALCON_PUBLIC_ADDRESS,
                    "--mainnet-vkey",
                    main_v,
                    "--testnet-vkey",
                    test_v,
                    "--compromised-vkey-digest",
                    log_identity.public_string_digest(compromised),
                ]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("MAINNET_FALCON_IDENTITY_DISTINCT ok", out)
        self.assertIn("MAINNET_VKEY_NE_TESTNET ok", out)
        self.assertIn("MAINNET_VKEY_NE_COMPROMISED ok", out)
        self.assertNotIn("BEGIN PRIVATE KEY", out)

    def test_collision_fails(self):
        mod = _load("pq_public_identity_check")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = mod.main(
                [
                    "--mainnet-address",
                    log_identity.TESTNET_FALCON_PUBLIC_ADDRESS,
                    "--testnet-address",
                    log_identity.TESTNET_FALCON_PUBLIC_ADDRESS,
                ]
            )
        self.assertEqual(rc, 1)
        self.assertIn("MAINNET_FALCON_IDENTITY_DISTINCT fail", buf.getvalue())


class FreshStateScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        self.mod = _load("pq_log_fresh_state")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        clear_pq_env()
        self.tmp.cleanup()

    def test_init_empty_and_refuse_overwrite(self):
        dest = Path(self.tmp.name) / "pq-log-mainnet.sqlite"
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = self.mod.main(["--init-empty", "--dest", str(dest)])
        self.assertEqual(rc, 0)
        self.assertIn("leaves=0", buf.getvalue())
        self.assertIn("fresh=yes", buf.getvalue())
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = str(dest)
        store.reset()
        store.append(b"keep-out")
        store.close()
        with self.assertRaises(SystemExit):
            self.mod.init_empty(dest)
        counts = self.mod.inspect_counts(dest)
        self.assertEqual(counts["leaves"], 1)
        self.assertFalse(self.mod.is_fresh(counts))


if __name__ == "__main__":
    unittest.main()
