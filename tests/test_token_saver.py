"""End-to-end tests: init → index → search → retrieve → summarize → MCP."""
from __future__ import annotations

import json
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from token_saver.config import init_workspace, load_config
from token_saver.cli import main as cli_main
from token_saver.indexer import index_workspace
from token_saver.mcp_server import Server
from token_saver.retrieval import get_source_slice, retrieve_context, search
from token_saver.summarize import summarize_file, summarize_folder
from token_saver.stats import iter_events, workspace_log_path
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
        self.assertIn("stats", names)
        call = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "retrieve_context",
                                      "arguments": {"task": "renewal obligations"}}})
        text = call["result"]["content"][0]["text"]
        self.assertIn("contract.md", text)
        bad = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "nope", "arguments": {}}})
        self.assertTrue(bad["result"].get("isError"))

    def test_cli_and_mcp_record_tool_counterfactuals_once(self):
        log = workspace_log_path(self.tmp)
        log.unlink(missing_ok=True)

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["search", "renewal", str(self.tmp)]), 0)
        self.assertIn("contract.md", output.getvalue())

        srv = Server(str(self.tmp))
        calls = [
            ("retrieve_context", {"task": "renewal obligations"}),
            ("semantic_search", {"query": "authenticate credentials"}),
            ("summarize_file", {"file": "docs/contract.md"}),
            ("summarize_folder", {"folder": "docs"}),
        ]
        for index, (name, arguments) in enumerate(calls, 1):
            response = srv.handle({
                "jsonrpc": "2.0", "id": index, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            self.assertFalse(response["result"].get("isError"))

        rows = list(iter_events(log))
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            [row["tool"] for row in rows],
            ["semantic_search", *[name for name, _ in calls]],
        )
        self.assertTrue(all(row["counterfactual_tokens"] > 0 for row in rows))
        self.assertTrue(all(row["returned_tokens"] > 0 for row in rows))

        before = log.read_bytes()
        stats_response = srv.handle({
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "stats", "arguments": {}},
        })
        self.assertIn("retrieval tools", stats_response["result"]["content"][0]["text"])
        self.assertEqual(log.read_bytes(), before)


def make_minimal_pdf(text: str) -> bytes:
    """Handcraft a one-page PDF with real extractable text (no deps)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


class FixesAndPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_index(self):
        init_workspace(self.tmp)
        return index_workspace(self.tmp)

    def test_multipart_ignore_patterns(self):
        (self.tmp / "docs" / "private").mkdir(parents=True)
        (self.tmp / "docs" / "private" / "secret.md").write_text("# classified renewal",
                                                                 encoding="utf-8")
        (self.tmp / "docs" / "public.md").write_text("# public renewal", encoding="utf-8")
        (self.tmp / "rootonly").mkdir()
        (self.tmp / "rootonly" / "x.md").write_text("# rootonly doc", encoding="utf-8")
        (self.tmp / "sub" / "rootonly").mkdir(parents=True)
        (self.tmp / "sub" / "rootonly" / "y.md").write_text("# nested rootonly doc",
                                                            encoding="utf-8")
        init_workspace(self.tmp)
        (self.tmp / ".tokensaverignore").write_text(
            "docs/private/\n/rootonly/\n", encoding="utf-8")
        index_workspace(self.tmp)
        paths = {h.path for h in search(self.tmp, "renewal rootonly doc", top_k=20)}
        self.assertIn("docs/public.md", paths)
        self.assertNotIn("docs/private/secret.md", paths)   # multi-part dir pattern
        self.assertNotIn("rootonly/x.md", paths)            # root-anchored
        self.assertIn("sub/rootonly/y.md", paths)           # anchor must not over-match

    def test_toml_escaping(self):
        from token_saver.install import _toml_str
        self.assertEqual(_toml_str('C:\\Users\\armaa'), '"C:\\\\Users\\\\armaa"')
        self.assertEqual(_toml_str('pa"th'), '"pa\\"th"')

    def test_config_scalar_section_keeps_defaults(self):
        init_workspace(self.tmp)
        (self.tmp / ".tokensaver" / "config.json").write_text(
            '{"retrieval": "invalid"}', encoding="utf-8")
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["retrieval"]["max_context_tokens"], 8000)

    def test_summarize_abs_path_outside_root(self):
        from token_saver.cli import main as cli_main
        self._init_index()
        rc = cli_main(["summarize", "/etc/passwd", str(self.tmp)])
        self.assertEqual(rc, 1)  # clean error, no traceback

    def test_mcp_int_coercion(self):
        (self.tmp / "a.md").write_text("# renewal terms apply here", encoding="utf-8")
        self._init_index()
        srv = Server(str(self.tmp))
        ok = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "retrieve_context",
                                    "arguments": {"task": "renewal", "max_tokens": "600"}}})
        self.assertFalse(ok["result"].get("isError"))
        bad = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "retrieve_context",
                                     "arguments": {"task": "renewal", "max_tokens": "lots"}}})
        self.assertTrue(bad["result"].get("isError"))
        self.assertIn("must be an integer", bad["result"]["content"][0]["text"])

    def test_mtime_short_circuit_and_metadata_drift(self):
        f = self.tmp / "a.md"
        f.write_text("# renewal terms", encoding="utf-8")
        self._init_index()
        stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 0)
        # touch (metadata drift, same content) — must not re-chunk
        import os as _os
        _os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
        stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 0)

    def test_pdf_to_md_to_vector_pipeline(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed")
        (self.tmp / "contract.pdf").write_bytes(
            make_minimal_pdf("Renewal obligations require sixty days notice"))
        stats = self._init_index()
        self.assertGreater(stats["vectors"], 0)
        md = self.tmp / ".tokensaver" / "converted" / "contract.pdf.md"
        self.assertTrue(md.exists())                      # PDF -> Markdown mirror
        self.assertIn("## Page 1", md.read_text(encoding="utf-8"))
        hits = search(self.tmp, "renewal obligations notice")
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "contract.pdf")    # cites the ORIGINAL pdf
        self.assertEqual(hits[0].page, 1)                 # with the page number

    def test_vectors_and_semantic_scoring(self):
        from token_saver.vectors import cosine, embed
        a = embed("automatic contract renewal with notice period")
        b = embed("the contract renews automatically unless notice is given")
        c = embed("goroutine channel scheduler preemption")
        self.assertGreater(cosine(a, b), cosine(a, c))
        self.assertAlmostEqual(cosine(a, a), 1.0, places=5)

    def test_vector_fallback_gate_tiebreak_and_text_fetch(self):
        """E02: the bounded-heap fallback must reproduce the old
        materialize-everything-then-sort semantics exactly -- same gate,
        same score ordering, same ascending-chunk-id tie-break -- and must
        fetch the correct text for each surviving winner, not a swapped one.
        """
        from array import array

        from token_saver.indexer import connect
        from token_saver.retrieval import _vector_fallback_search
        from token_saver.vectors import to_blob

        con = connect(self.tmp)

        def make_vec(x0):
            v = array("f", [0.0] * 384)
            v[0] = x0
            return v

        # (path, cosine-along-dim0): three-way tie at 0.9, one clear winner
        # at 0.95, one below the hashed_tf gate (0.35) that must be excluded.
        specs = [("a.md", 0.9), ("b.md", 0.9), ("c.md", 0.2),
                 ("d.md", 0.9), ("e.md", 0.95)]
        cid_by_path = {}
        for i, (path, x0) in enumerate(specs):
            fcur = con.execute(
                "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) "
                "VALUES (?,?,?,?,?,?)", (path, f"sha{i}", 0.0, 10, "md", 5))
            ccur = con.execute(
                "INSERT INTO chunks(file_id, path, section, heading_path, start_line, "
                "end_line, page, text, ntokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (fcur.lastrowid, path, "", "", 1, 1, None, f"text for {path}", 5))
            cid_by_path[path] = ccur.lastrowid
            con.execute("INSERT INTO vectors(chunk_id, vec) VALUES (?,?)",
                        (ccur.lastrowid, to_blob(make_vec(x0))))
        con.commit()

        qvec = array("f", [1.0] + [0.0] * 383)  # dot product == x0 directly

        class StubEmbedder:
            name = "hashed_tf"

        hits = _vector_fallback_search(con, qvec, set(), StubEmbedder(), top_k=3)
        con.close()

        self.assertEqual(len(hits), 3)
        self.assertNotIn("c.md", {h.path for h in hits})  # below gate
        self.assertEqual(hits[0].path, "e.md")             # clear winner first
        for h in hits:
            self.assertEqual(h.text, f"text for {h.path}")  # winner text not swapped

        # Tie group {a,b,d} at cos 0.9 has 2 remaining slots -- ascending
        # chunk id wins, matching the old stable-sort-by-score behavior.
        tied_ids = sorted(cid_by_path[p] for p in ("a.md", "b.md", "d.md"))
        kept_tied = sorted(h.chunk_id for h in hits if h.path != "e.md")
        self.assertEqual(kept_tied, tied_ids[:2])


if __name__ == "__main__":
    unittest.main()
