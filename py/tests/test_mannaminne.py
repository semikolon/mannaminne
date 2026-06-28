import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class ChunkingTests(unittest.TestCase):
    def test_char_chunk_overlap_and_limit(self):
        chunks = list(m.chunk("abcdefghij", size=4, overlap=1, max_chunks=3))
        self.assertEqual(chunks, [(0, "abcd"), (1, "defg"), (2, "ghij")])

    def test_markdown_chunks_repeat_heading_path(self):
        text = "# Alpha\n" + ("one two three four five " * 20) + "\n## Beta\nBeta body"
        chunks = list(m.chunk_markdown(text, size=90, overlap=10, max_chunks=20))
        alpha_chunks = [c for _, c in chunks if "one two" in c or "three four" in c]
        self.assertTrue(alpha_chunks)
        self.assertTrue(all(c.startswith("Heading: Alpha\n\n") for c in alpha_chunks))
        self.assertTrue(any(c.startswith("Heading: Alpha > Beta\n\n") for _, c in chunks))

    def test_rows_strip_nuls_and_hash_chunk_text(self):
        rows = list(m._rows("doc", "doc:x", "proj", "T\x00itle", "a\x00b", "",
                            chunker=lambda _full: [(0, "a\x00b")]))
        self.assertEqual(rows[0][5], "Title")
        self.assertEqual(rows[0][6], "ab")
        self.assertEqual(rows[0][8], m.h("ab"))


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.old = (
            m._EMBED_URL_CACHE, m.EXPLICIT_EMBED_URL, m.Z4_EMBED_URL,
            m.DARWIN_EMBED_URL, m.EMBED_DIM, m.EMBED_PROBE_TIMEOUT,
            m.EMBED_TIMEOUT,
        )
        m._EMBED_URL_CACHE = None
        m.EXPLICIT_EMBED_URL = None
        m.Z4_EMBED_URL = "http://z4.local/v1/embeddings"
        m.DARWIN_EMBED_URL = "http://darwin.local/v1/embeddings"
        m.EMBED_DIM = 3
        m.EMBED_PROBE_TIMEOUT = 0.5
        m.EMBED_TIMEOUT = 12.0
        # Isolate the Z4 cooldown file so a real cooldown left by live embed runs
        # doesn't make these Z4-preference tests skip Z4.
        self._old_cooldown_file = m.Z4_COOLDOWN_FILE
        m.Z4_COOLDOWN_FILE = os.path.join(tempfile.gettempdir(), "mannaminne-test-nocooldown")
        m._z4_clear_cooldown()

    def tearDown(self):
        (
            m._EMBED_URL_CACHE, m.EXPLICIT_EMBED_URL, m.Z4_EMBED_URL,
            m.DARWIN_EMBED_URL, m.EMBED_DIM, m.EMBED_PROBE_TIMEOUT,
            m.EMBED_TIMEOUT,
        ) = self.old
        m.Z4_COOLDOWN_FILE = self._old_cooldown_file

    def test_embed_batch_prefers_z4_when_available(self):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append((req.full_url, timeout, json.loads(req.data.decode())))
            return FakeResponse({"data": [{"embedding": [1, 2, 3, 4]}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(m._embed_batch(["hello"]), [[1, 2, 3]])

        self.assertEqual(calls[0][0], "http://z4.local/v1/embeddings")
        self.assertEqual(m._EMBED_URL_CACHE, "http://z4.local/v1/embeddings")

    def test_embed_batch_falls_back_to_darwin(self):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append((req.full_url, timeout))
            if req.full_url == "http://z4.local/v1/embeddings":
                raise TimeoutError("z4 unavailable")
            return FakeResponse({"data": [{"embedding": [4, 5, 6, 7]}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(m._embed_batch(["hello"]), [[4, 5, 6]])

        self.assertEqual(calls, [
            ("http://z4.local/v1/embeddings", 0.5),
            ("http://darwin.local/v1/embeddings", 12.0),
        ])
        self.assertEqual(m._EMBED_URL_CACHE, "http://darwin.local/v1/embeddings")

    def test_embed_batch_retries_z4_after_darwin_cache(self):
        m._EMBED_URL_CACHE = "http://darwin.local/v1/embeddings"
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req.full_url)
            return FakeResponse({"data": [{"embedding": [7, 8, 9, 10]}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(m._embed_batch(["hello"]), [[7, 8, 9]])

        self.assertEqual(calls, ["http://z4.local/v1/embeddings"])
        self.assertEqual(m._EMBED_URL_CACHE, "http://z4.local/v1/embeddings")

    def test_embed_pairs_splits_failed_batches(self):
        def fake_embed(texts):
            if len(texts) > 1:
                raise RuntimeError("batch too large")
            return [[len(texts[0])]]

        with mock.patch.object(m, "_embed_batch", fake_embed):
            out = m._embed_pairs([("a", "hello"), ("b", "world!")])

        self.assertEqual(out, [("a", [5]), ("b", [6])])


class RankingAndEvalTests(unittest.TestCase):
    def row(self, title):
        return ("id", "doc", "proj", title, "text", "")

    def source_row(self, source_id, title):
        return (f"{source_id}#0", "doc", "proj", title, "text", "")

    def test_query_terms_drop_filler_and_soft_terms_drop_generic_search_words(self):
        self.assertEqual(
            m._query_terms("what did I say about local codebase vector search?"),
            ["local", "codebase", "vector", "search"],
        )
        self.assertEqual(
            m._soft_terms("local codebase vector search"),
            ["codebase", "vector"],
        )

    def test_fusion_keeps_exact_keyword_above_semantic_only(self):
        ranked = m._fuse_ranked({
            "sem": {"r": self.row("semantic generic"), "kw": False, "sem": 0.99, "sem_rank": 1},
            "kw": {"r": self.row("exact needle"), "kw": True, "sem": 0.2, "kw_rank": 1, "exact": True},
        }, limit=2)
        self.assertEqual(ranked[0]["r"][3], "exact needle")

    def test_fusion_deduplicates_source_objects(self):
        ranked = m._fuse_ranked({
            "doc:one#0": {"r": self.source_row("doc:one", "first chunk"), "kw": True, "kw_rrf": 0.2},
            "doc:one#1": {"r": self.source_row("doc:one", "second chunk"), "kw": True, "kw_rrf": 0.1},
            "doc:two#0": {"r": self.source_row("doc:two", "other source"), "kw": True, "kw_rrf": 0.05},
        }, limit=3)
        self.assertEqual([r["r"][3] for r in ranked], ["first chunk", "other source"])

    def test_expectation_matching_supports_strings_and_fields(self):
        result = {"r": ("id:1", "doc", "dotfiles", "CLAUDE.md", "mannaminne canonical", "")}
        self.assertTrue(m._expectation_matches(result, "canonical"))
        self.assertTrue(m._expectation_matches(result, {"title": "CLAUDE.md", "project": "dotfiles"}))
        self.assertFalse(m._expectation_matches(result, {"title": "other"}))


class SessionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("MANNAMINNE_SESSION_PATHS")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MANNAMINNE_SESSION_PATHS", None)
        else:
            os.environ["MANNAMINNE_SESSION_PATHS"] = self._old_env

    @staticmethod
    def _write_session(root, projdir, sid, user_text):
        d = Path(root) / projdir
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "user", "timestamp": "2026-05-01T10:00:00Z",
                           "message": {"content": user_text}})
        (d / f"{sid}.jsonl").write_text(line + "\n", encoding="utf-8")

    def test_session_paths_env_override_splits_pathsep_live_first(self):
        os.environ["MANNAMINNE_SESSION_PATHS"] = f"/a/live{os.pathsep}/b/archive"
        self.assertEqual(m._session_paths(), ["/a/live", "/b/archive"])

    def test_discover_sessions_multi_path_dedup_live_wins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as live, tempfile.TemporaryDirectory() as arch:
            # same sid in both roots, different text → live must win
            self._write_session(live, "proj-alpha", "dup-sid", "needle LIVE copy")
            self._write_session(arch, "proj-alpha", "dup-sid", "needle ARCHIVE copy")
            # archive-only session must still be discovered
            self._write_session(arch, "proj-beta", "arch-only", "needle ARCHIVE only")
            os.environ["MANNAMINNE_SESSION_PATHS"] = f"{live}{os.pathsep}{arch}"
            rows = list(m.discover_sessions())
            texts = " ".join(r[6] for r in rows)
            self.assertIn("LIVE copy", texts)
            self.assertNotIn("ARCHIVE copy", texts)          # dup deduped, live won
            self.assertIn("ARCHIVE only", texts)             # archive-unique kept
            sids = {r[2] for r in rows}
            self.assertEqual(sids, {"session:dup-sid", "session:arch-only"})

    def test_discover_sessions_raises_on_unmounted_volume(self):
        os.environ["MANNAMINNE_SESSION_PATHS"] = "/Volumes/FERMI/claude-sessions-archive"
        with mock.patch.object(m, "_volume_available", return_value=False):
            with self.assertRaises(m.SourceUnavailable):
                list(m.discover_sessions())

    def test_volume_available_for_local_path_is_true(self):
        self.assertTrue(m._volume_available("/Users/x/.claude/projects"))


class IngestPruneSafetyTests(unittest.TestCase):
    def test_unavailable_kind_skipped_and_excluded_from_prune(self):
        recorded = []

        class FakeCopy:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def write_row(self, _r): pass

        class FakeCur:
            rowcount = 0
            def execute(self, sql, params=None): recorded.append((sql, params))
            def executemany(self, sql, rows): recorded.append((sql, list(rows)))
            def copy(self, _sql): return FakeCopy()

        class FakeConn:
            def cursor(self): return FakeCur()
            def commit(self): pass
            def rollback(self): pass

        def raising():
            raise m.SourceUnavailable("test unmount")
            yield  # pragma: no cover — makes this a generator

        def normal():
            yield ("doc:one#0", "doc", "doc:one", 0, "proj", "T", "body", "", "hash")

        args = type("A", (), {"sources": ["session", "doc"]})()
        with mock.patch.object(m, "load_conn", return_value=FakeConn()), \
             mock.patch.object(m, "ALL", {"session": raising, "doc": normal}):
            m.cmd_ingest(args)

        deletes = [(s, p) for (s, p) in recorded if isinstance(s, str) and s.startswith("DELETE FROM chunks")]
        self.assertEqual(len(deletes), 1)
        # prune scoped to the COMPLETED kind only — 'session' (unmounted) excluded
        self.assertEqual(deletes[0][1], (["doc"],))


class EmbedEndpointCooldownTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._old_file = m.Z4_COOLDOWN_FILE
        self._old_cache = m._EMBED_URL_CACHE
        m.Z4_COOLDOWN_FILE = os.path.join(self._tmp, "z4-cooldown")
        m._EMBED_URL_CACHE = None

    def tearDown(self):
        m.Z4_COOLDOWN_FILE = self._old_file
        m._EMBED_URL_CACHE = self._old_cache

    def test_cooldown_set_clear_roundtrip(self):
        self.assertFalse(m._z4_in_cooldown())
        m._z4_set_cooldown()
        self.assertTrue(m._z4_in_cooldown())
        m._z4_clear_cooldown()
        self.assertFalse(m._z4_in_cooldown())

    def test_embed_batch_tries_z4_first_when_not_cooled(self):
        if m.EXPLICIT_EMBED_URL:
            self.skipTest("MANNAMINNE_EMBED_URL set in env")
        m._z4_clear_cooldown()
        calls = []
        with mock.patch.object(m, "_post_embed",
                               side_effect=lambda url, texts, timeout: (calls.append(url), [[0.0]] * len(texts))[1]):
            m._embed_batch(["x"])
        self.assertEqual(calls[0], m.Z4_EMBED_URL)   # Z4 probed first

    def test_embed_batch_skips_z4_when_in_cooldown(self):
        if m.EXPLICIT_EMBED_URL:
            self.skipTest("MANNAMINNE_EMBED_URL set in env")
        m._z4_set_cooldown()
        calls = []
        with mock.patch.object(m, "_post_embed",
                               side_effect=lambda url, texts, timeout: (calls.append(url), [[0.0]] * len(texts))[1]):
            m._embed_batch(["x"])
        self.assertNotIn(m.Z4_EMBED_URL, calls)      # Z4 skipped during cooldown
        self.assertIn(m.DARWIN_EMBED_URL, calls)

    def test_z4_failure_sets_cooldown(self):
        if m.EXPLICIT_EMBED_URL:
            self.skipTest("MANNAMINNE_EMBED_URL set in env")
        m._z4_clear_cooldown()

        def post(url, texts, timeout):
            if url == m.Z4_EMBED_URL:
                raise ConnectionRefusedError("z4 down")
            return [[0.0]] * len(texts)

        with mock.patch.object(m, "_post_embed", side_effect=post):
            m._embed_batch(["x"])                     # Z4 fails → Darwin succeeds
        self.assertTrue(m._z4_in_cooldown())          # failure recorded a cooldown


class SourceFingerprintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old = m._FINGERPRINT_FILE
        m._FINGERPRINT_FILE = os.path.join(self._tmp, "fp.json")
        m._PENDING_FINGERPRINTS.clear()

    def tearDown(self):
        m._FINGERPRINT_FILE = self._old
        m._PENDING_FINGERPRINTS.clear()

    def _mk(self, name, content="x"):
        p = os.path.join(self._tmp, name)
        with open(p, "w") as fh:
            fh.write(content)
        return p

    def test_fingerprint_changes_on_size(self):
        p = self._mk("a.txt", "hello")
        fp1 = m._fingerprint_paths([p])
        with open(p, "w") as fh:
            fh.write("hello world much longer now")
        self.assertNotEqual(fp1, m._fingerprint_paths([p]))

    def test_skip_raises_only_after_save_when_unchanged(self):
        p = self._mk("b.txt", "data")
        m._skip_if_unchanged("doc", [p])                 # no stored fp → proceeds
        self.assertIn("doc", m._PENDING_FINGERPRINTS)
        m._save_fingerprint("doc", m._PENDING_FINGERPRINTS["doc"])
        with self.assertRaises(m.SourceUnchanged):       # unchanged → skip
            m._skip_if_unchanged("doc", [p])

    def test_skip_proceeds_when_changed(self):
        p = self._mk("c.txt", "v1")
        m._skip_if_unchanged("note", [p])
        m._save_fingerprint("note", m._PENDING_FINGERPRINTS["note"])
        with open(p, "w") as fh:
            fh.write("v2 different length")
        m._skip_if_unchanged("note", [p])                # changed → must NOT raise

    def test_empty_paths_never_skips(self):
        m._save_fingerprint("email", m._fingerprint_paths([]))
        m._skip_if_unchanged("email", [])                # empty inputs must NOT skip

    def test_cmd_ingest_skips_unchanged_kind_and_excludes_from_prune(self):
        recorded = []

        class FakeCopy:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def write_row(self, _r): pass

        class FakeCur:
            rowcount = 0
            def execute(self, sql, params=None): recorded.append((sql, params))
            def executemany(self, sql, rows): recorded.append((sql, list(rows)))
            def copy(self, _sql): return FakeCopy()

        class FakeConn:
            def cursor(self): return FakeCur()
            def commit(self): pass
            def rollback(self): pass

        def unchanged():
            raise m.SourceUnchanged("doc")
            yield  # pragma: no cover

        def normal():
            yield ("x:1", "x", "x:1", 0, "p", "T", "body", "", "hh")

        args = type("A", (), {"sources": ["doc", "x"]})()
        with mock.patch.object(m, "load_conn", return_value=FakeConn()), \
             mock.patch.object(m, "ALL", {"doc": unchanged, "x": normal}):
            m.cmd_ingest(args)
        deletes = [(s, p) for (s, p) in recorded if isinstance(s, str) and s.startswith("DELETE FROM chunks")]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], (["x"],))        # 'doc' (unchanged) excluded from prune


if __name__ == "__main__":
    unittest.main()
