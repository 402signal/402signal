"""Static regression: router must not dump secrets or os.environ."""

from __future__ import annotations

import ast
import os
import re
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import store, worker
from live402.pq import transparency as pq_view
from live402.server import Handler

ROOT = Path(__file__).resolve().parents[1]
APP_ROOTS = (ROOT / "live402", ROOT / "scripts", ROOT / ".github")

_DUMP_SUBSTRINGS = (
    "dict(os.environ",
    "repr(os.environ",
    "os.environ.items(",
    "print(os.environ",
    "json.dumps(os.environ",
    "json.dumps(dict(os.environ",
    "os.environ.copy()",
    "list(os.environ",
    "vars(os.environ",
)

_SECRET_ENV_NAMES = (
    "CDP_ACCESS_TOKEN",
    "CDP_API_KEY_SECRET",
    "CDP_KEY_SECRET",
    "PAYAI_ACCESS_TOKEN",
    "PAYAI_API_KEY",
    "LIVE402_PQ_FALCON_SK",
    "LIVE402_PQ_LOG_SK",
    "LIVE402_PQ_LOG_SK_MAINNET",
    "LIVE402_PQ_SIGNER_TOKEN",
    "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
    "LIVE402_HMAC",
    "HMAC_SECRET",
    "LIVE402_PQ_CONFIRM_TATUM_API_KEY",
    "LIVE402_PQ_CONFIRM_NOWNODES_API_KEY",
    "LIVE402_PQ_CONFIRM_INDEXER_TOKEN",
)

_ITER_ENV_ATTRS = frozenset({"items", "values", "keys", "copy"})


def _iter_py_files():
    for root in APP_ROOTS:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.suffix in {".py", ".yml", ".yaml", ".sh", ".toml"} and path.is_file():
                yield path


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SecretDumpBanTests(unittest.TestCase):
    def test_no_environ_dump_constructs_in_app_scripts_or_ci(self):
        hits = []
        for path in _iter_py_files():
            text = _source(path)
            for needle in _DUMP_SUBSTRINGS:
                if needle in text:
                    hits.append("%s: %s" % (path.relative_to(ROOT), needle))
        self.assertEqual(hits, [])

    def test_python_ast_forbids_environ_iteration_and_repr(self):
        violations = []
        for path in (ROOT / "live402").rglob("*.py"):
            tree = ast.parse(_source(path), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in _ITER_ENV_ATTRS and _is_os_environ(node.func.value):
                        violations.append("%s:%s os.environ.%s()" % (path.name, node.lineno, node.func.attr))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"dict", "repr", "list", "print"} and node.args:
                        arg0 = node.args[0]
                        if _is_os_environ(arg0):
                            violations.append("%s:%s %s(os.environ)" % (path.name, node.lineno, node.func.id))
        self.assertEqual(violations, [])

    def test_transparency_secret_markers_cover_router_credentials(self):
        markers = pq_view._SECRET_MARKERS
        for name in _SECRET_ENV_NAMES:
            self.assertIn(name, markers, name)

    def test_scripts_do_not_print_secret_env_gets(self):
        pattern = re.compile(
            r"print\([^)]*os\.environ\.(?:get|__getitem__)",
            re.MULTILINE,
        )
        hits = []
        for path in (ROOT / "scripts").glob("*.py"):
            text = _source(path)
            if pattern.search(text):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


class SecretResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        return res.status, body

    def test_health_ready_transparency_omit_secret_names(self):
        for path in ("/health", "/ready", "/transparency"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            for name in _SECRET_ENV_NAMES:
                self.assertNotIn(name, body, path)
            self.assertNotIn("BEGIN PRIVATE KEY", body)
            self.assertNotIn("os.environ", body)


if __name__ == "__main__":
    unittest.main()
