"""End-to-end tests: init → index → search → retrieve → summarize → MCP."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from token_saver.config import init_workspace, load_config
from token_saver.indexer import index_workspace
from token_saver.mcp_server import Server
from token_saver.retrieval import get_source_slice, retrieve_context, search
from token_saver.summarize import summarize_file, summarize_folder
from token_saver.workspace import resolve_workspace


def make_fixture(root: Path) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "node_modules" / "junk").mkdir(parents=True)
    (root / "docs" / "contract.md").write_text(
        "# Master Agreement\n\n## 4.2 Renewal Term\n\n"
        "The agreement renews automatically unless either party gives sixty days "
        "notice before the renewal date. The renewal obligation binds both parties.\n\n"
        "## 9.1 Termination\n\nEither party may terminate for material breach.\n",
        encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "def authenticate(user, password):\n    \"\"\"Check credentials.\"\"\"\n"
        "    return user == 'admin'\n\n\nclass SessionManager:\n"
        "    def refresh_token(self):\n        return 'token'\n",
        encoding="utf-8")
    (root / "data.csv").write_text("name,amount\nalice,10\nbob,20\n", encoding="utf-8")
    (root / "node_modules" / "junk" / "lib.js").write_text("var x = 1;", encoding="utf-8")
    (root / ".env").write_text("SECRET=donotindex", encoding="utf-8")


class TokenSaverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        make_fixture(cls.tmp)
        init_workspace(cls.tmp)
        cls.stats = index_workspace(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_index_stats(self):
        self.assertGreaterEqual(self.stats["files"], 3)
        self.assertGreater(self.stats["indexed_tokens"], 0)

    def test_ignores_respected(self):
        hits = search(self.tmp, "donotindex")
        self.assertEqual(hits, [])
        hits = search(self.tmp, "var x lib")
        self.assertFalse(any("node_modules" in h.path for h in hits))

    def test_search_finds_renewal(self):
        hits = search(self.tmp, "renewal obligations notice")
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "docs/contract.md")
        self.assertIn("Renewal", hits[0].section)

    def test_search_finds_code_symbol(self):
        hits = search(self.tmp, "authenticate credentials")
        self.assertTrue(any(h.path == "src/auth.py" for h in hits))

    def test_retrieve_context_pack(self):
        pack = retrieve_context(self.tmp, "summarize the renewal obligations")
        self.assertIn("evidence, not", pack)
        self.assertIn("docs/contract.md", pack)
        self.assertIn("sixty days", pack)

    def test_budget_respected(self):
        pack = retrieve_context(self.tmp, "renewal termination authenticate", max_tokens=100)
        self.assertLess(len(pack), 100 * 4 + 1500)  # budget + envelope overhead

    def test_summaries(self):
        s = summarize_file(self.tmp, "docs/contract.md", focus="renewal")
        self.assertIn("Renewal Term", s)
        f = summarize_folder(self.tmp)
        self.assertIn("files", f)

    def test_source_slice_and_escape(self):
        out = get_source_slice(self.tmp, "src/auth.py", 1, 3)
        self.assertIn("authenticate", out)
        with self.assertRaises(ValueError):
            get_source_slice(self.tmp, "../outside.txt")

    def test_incremental_reindex(self):
        stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 0)  # nothing changed
        (self.tmp / "docs" / "new.md").write_text("# Arbitration clause\n", encoding="utf-8")
        stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 1)
        self.assertTrue(search(self.tmp, "arbitration"))

    def test_workspace_resolver(self):
        sub = self.tmp / "docs"
        self.assertEqual(resolve_workspace(cwd=str(sub)), self.tmp.resolve())

    def test_config_merge(self):
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["retrieval"]["max_chunks"], 12)

    def test_mcp_server(self):
        srv = Server(str(self.tmp))
        init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "token-saver")
        tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertIn("retrieve_context", names)
        call = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "retrieve_context",
                                      "arguments": {"task": "renewal obligations"}}})
        text = call["result"]["content"][0]["text"]
        self.assertIn("contract.md", text)
        bad = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "nope", "arguments": {}}})
        self.assertTrue(bad["result"].get("isError"))


if __name__ == "__main__":
    unittest.main()
