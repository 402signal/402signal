"""Asset URL fingerprinting. Presentation only. No public SHA endpoint."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live402 import asset_version


class AssetVersionTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("LIVE402_ASSET_VERSION")
        os.environ.pop("LIVE402_ASSET_VERSION", None)
        asset_version.reset_for_tests()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("LIVE402_ASSET_VERSION", None)
        else:
            os.environ["LIVE402_ASSET_VERSION"] = self._prev
        asset_version.reset_for_tests()

    def test_env_override_is_used(self):
        os.environ["LIVE402_ASSET_VERSION"] = "abc123def456"
        asset_version.reset_for_tests()
        self.assertEqual(asset_version.asset_version(), "abc123def456")

    def test_git_sha_from_this_checkout(self):
        ver = asset_version.asset_version()
        self.assertRegex(ver, r"^[0-9a-f]{7,40}$")
        self.assertNotIn("FLY_IMAGE_REF", ver)
        self.assertNotIn("registry.fly", ver)

    def test_rejects_fly_image_ref_shaped_env(self):
        os.environ["LIVE402_ASSET_VERSION"] = "registry.fly.io/402signal:deployment-01h"
        asset_version.reset_for_tests()
        ver = asset_version.asset_version()
        self.assertNotIn("registry.fly.io", ver)
        self.assertNotIn("deployment-", ver)

    def test_ignores_fly_image_ref_process_env(self):
        os.environ["FLY_IMAGE_REF"] = "registry.fly.io/402signal:deployment-01h"
        try:
            asset_version.reset_for_tests()
            ver = asset_version.asset_version()
            self.assertNotEqual(ver, os.environ["FLY_IMAGE_REF"])
            self.assertNotIn("registry.fly.io", ver)
        finally:
            os.environ.pop("FLY_IMAGE_REF", None)

    def test_stamp_html_versions_known_assets_once(self):
        os.environ["LIVE402_ASSET_VERSION"] = "deadbeefcafebabe"
        asset_version.reset_for_tests()
        html = (
            '<link rel="stylesheet" href="/styles.css" />'
            '<script src="/app.js"></script>'
            '<script src="/dashboard.js"></script>'
            '<script src="/transparency.js"></script>'
        )
        stamped = asset_version.stamp_html(html)
        self.assertIn("/styles.css?v=deadbeefcafebabe", stamped)
        self.assertIn("/app.js?v=deadbeefcafebabe", stamped)
        self.assertIn("/dashboard.js?v=deadbeefcafebabe", stamped)
        self.assertIn("/transparency.js?v=deadbeefcafebabe", stamped)
        self.assertEqual(stamped, asset_version.stamp_html(stamped))
        self.assertNotIn('href="/styles.css"', stamped)

    def test_reads_asset_version_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".asset-version"
            path.write_text("feedfacecafecafe\n", encoding="utf-8")
            with patch.object(asset_version, "_version_files", return_value=[path]):
                with patch.object(asset_version, "_git_rev_parse", return_value=""):
                    with patch.object(asset_version, "_git_head_file", return_value=""):
                        asset_version.reset_for_tests()
                        self.assertEqual(asset_version.asset_version(), "feedfacecafecafe")

    def test_source_does_not_expose_public_sha_endpoint(self):
        root = Path(__file__).resolve().parent.parent
        server = (root / "live402" / "server.py").read_text(encoding="utf-8")
        av = (root / "live402" / "asset_version.py").read_text(encoding="utf-8")
        self.assertNotIn('os.environ.get("FLY_IMAGE_REF")', server)
        self.assertNotIn('os.environ.get("FLY_IMAGE_REF")', av)
        self.assertNotIn('os.environ["FLY_IMAGE_REF"]', server)
        self.assertNotIn('os.environ["FLY_IMAGE_REF"]', av)
        self.assertNotIn('"/build"', av)
        self.assertNotIn('"/status"', av)
        self.assertNotIn('"/sha"', av)


if __name__ == "__main__":
    unittest.main()
