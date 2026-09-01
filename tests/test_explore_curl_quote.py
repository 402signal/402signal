"""Explore-generated curl must safely quote arbitrary user text."""

from __future__ import annotations

import json
import os
import shlex
import unittest
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "live402" / "static" / "app.js").read_text(encoding="utf-8")


def shell_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


class ExploreCurlQuoteTests(unittest.TestCase):
    def test_app_js_uses_posix_single_quote(self):
        self.assertIn("function shellSingleQuote(value)", APP_JS)
        self.assertIn(".replace(/'/g, \"'\\\\''\")", APP_JS)
        self.assertIn("shellSingleQuote(compact)", APP_JS)
        self.assertNotRegex(
            APP_JS,
            r"""-d '" \+ compact \+ "'" """,
        )

    def test_arbitrary_user_text_survives_shlex(self):
        payloads = [
            {"need": "it's weather"},
            {"need": "it's a test; rm -rf / $(reboot) `id` $HOME"},
            {"need": "line1\nline2\t\"quotes\""},
            {"need": "weather", "max_price_usd": 0.05},
            {"need": ""},
        ]
        for body in payloads:
            compact = json.dumps(body, separators=(",", ":"))
            cmd = (
                "curl -sS -D - https://402signal.com/route "
                "-H 'Content-Type: application/json' -d " + shell_single_quote(compact)
            )
            parts = shlex.split(cmd)
            self.assertEqual(parts[-1], compact, body)
            self.assertEqual(parts[0], "curl")

    def test_unquoted_concatenation_would_break(self):
        compact = json.dumps({"need": "it's weather"}, separators=(",", ":"))
        unsafe = (
            "curl -sS -D - https://402signal.com/route "
            "-H 'Content-Type: application/json' -d '" + compact + "'"
        )
        with self.assertRaises(ValueError):
            shlex.split(unsafe)


if __name__ == "__main__":
    unittest.main()
