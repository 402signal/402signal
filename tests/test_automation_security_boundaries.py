"""Static checks for automation security boundaries. No Fly, no network."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "docs" / "automation-security-boundaries.md"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
PROTECTION = ROOT / "docs" / "github-protection.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AutomationSecurityBoundariesTests(unittest.TestCase):
    def test_boundaries_doc_exists(self):
        self.assertTrue(BOUNDARIES.is_file(), msg=str(BOUNDARIES))
        text = _read(BOUNDARIES)
        self.assertGreater(len(text.strip()), 0)
        self.assertNotIn("\u2014", text)

    def test_codeowners_ross_owns_pq(self):
        text = _read(CODEOWNERS)
        self.assertIn("@ross402signal", text)
        self.assertNotIn("@402signal/maintainers", text)
        owned = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if parts[0] in ("/live402/pq/", "live402/pq/") and "@ross402signal" in parts[1:]:
                owned = True
                break
        self.assertTrue(owned, msg="CODEOWNERS must assign @ross402signal for live402/pq/")

    def test_github_protection_does_not_claim_disabled(self):
        raw = _read(PROTECTION)
        text = " ".join(raw.lower().split())
        self.assertIn("protect main", text)
        self.assertIn("already enabled", text)
        self.assertNotIn("not implemented by this change", text)
        self.assertNotIn("@402signal/maintainers", text)
        self.assertNotIn("\u2014", raw)


if __name__ == "__main__":
    unittest.main()
