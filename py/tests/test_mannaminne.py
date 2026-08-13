import io
import json
import os
import sys
import tempfile
import time
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
        # Index 8 is `updated`, 9 is the content hash. The assertion below read
        # index 8 as the hash and had been failing since the created/updated split
        # added a column ahead of it (commit 2bf1bf3) — column drift, not a bug in
        # _rows. `updated` floors to `created` when not given, which is "" here.
        self.assertEqual(rows[0][8], "")
        self.assertEqual(rows[0][9], m.h("ab"))


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

    def test_post_embed_parses_context_exceeded_400(self):
        # llama.cpp returns HTTP 400 when a single input exceeds the server's
        # token context (Darwin embedder ctx=512). _post_embed must surface a
        # typed EmbedContextExceeded carrying the parsed budget, not a generic
        # error, so the caller can truncate-and-retry.
        import urllib.error
        body = (b'{"error":{"code":400,"message":"request (548 tokens) exceeds '
                b'the available context size (512 tokens), try increasing it"}}')

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(m.EmbedContextExceeded) as ctx:
                m._post_embed("http://darwin.local/v1/embeddings", ["x" * 750], 12.0)
        self.assertEqual(ctx.exception.budget_tokens, 512)
        self.assertEqual(ctx.exception.request_tokens, 548)

    def test_embed_batch_propagates_context_exceeded_over_generic(self):
        # z4 down (network) + darwin ctx-exceeded => the batch must raise the
        # typed EmbedContextExceeded (so _embed_pairs can truncate), NOT the
        # generic "all endpoints failed" RuntimeError.
        def fake_post(url, texts, timeout):
            if url == m.Z4_EMBED_URL:
                raise TimeoutError("z4 down")
            raise m.EmbedContextExceeded(budget_tokens=512, request_tokens=548)

        with mock.patch.object(m, "_post_embed", fake_post):
            with self.assertRaises(m.EmbedContextExceeded):
                m._embed_batch(["x" * 750])

    def test_embed_pairs_truncates_over_context_single_chunk(self):
        # A single over-context chunk must be shrunk to fit and embedded (head
        # semantic vector) rather than dropped to NULL forever. Simulate a
        # 512-ctx endpoint that rejects inputs longer than 500 chars.
        long = "x" * 750

        def fake_embed(texts):
            if len(texts[0]) > 500:
                raise m.EmbedContextExceeded(budget_tokens=512, request_tokens=550)
            return [[1, 2, 3]]

        with mock.patch.object(m, "_embed_batch", fake_embed):
            out = m._embed_pairs([("cid", long)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "cid")
        self.assertEqual(out[0][1], [1, 2, 3])

    def test_embed_pairs_records_drop_reason_not_misleading_default(self):
        # A persistent non-ctx failure must surface its real reason via
        # _LAST_DROP_REASON so cmd_embed stops mislabeling it "endpoint down".
        m._LAST_DROP_REASON = None

        def fake_embed(texts):
            raise RuntimeError("all embedding endpoints failed: boom")

        with mock.patch.object(m, "_embed_batch", fake_embed):
            out = m._embed_pairs([("cid", "hello")])
        self.assertEqual(out, [])
        self.assertIsNotNone(m._LAST_DROP_REASON)
        self.assertIn("boom", m._LAST_DROP_REASON)


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


class SimplenoteDateTests(unittest.TestCase):
    """The .txt export is dateless; the JSON sidecar carries creationDate +
    lastModified. These pin the join between them.

    The fixture's SHAPE is copied from the real export — CRLF line endings inside
    the JSON `content` field, and the `Tags:` footer that the .txt writer appends
    and the JSON does not carry. Those two details are the entire reason a naive
    equality join fails, so an invented fixture would have passed while matching
    nothing (see global CLAUDE.md, *a fixture is copied from reality*). The prose
    is substituted; the format is not.
    """

    def _export(self, tmp):
        d = Path(tmp)
        (d / "source").mkdir(parents=True)
        (d / "source" / "notes.json").write_text(json.dumps({
            "activeNotes": [
                {"id": "a1", "content": "Kort anteckning\r\n\r\nEn rad text.",
                 "creationDate": "2012-02-29T01:25:45.000Z",
                 "lastModified": "2019-04-18T09:00:00.000Z"},
                {"id": "b2", "content": "Utan tagg\r\n\r\nAnnan text.",
                 "creationDate": "2020-07-17T08:11:23.340Z",
                 "lastModified": "2020-07-17T08:11:23.340Z"},
            ],
            "trashedNotes": [
                {"id": "t3", "content": "Slängd\r\n\r\nSka inte synas.",
                 "creationDate": "2011-01-01T00:00:00.000Z",
                 "lastModified": "2011-01-01T00:00:00.000Z"},
            ],
        }), encoding="utf-8")
        # The .txt writer uses LF and appends the tag footer.
        (d / "Kort anteckning.txt").write_text(
            "Kort anteckning\n\nEn rad text.\n\nTags: cv\n", encoding="utf-8")
        (d / "Utan tagg.txt").write_text(
            "Utan tagg\n\nAnnan text.\n", encoding="utf-8")
        return d

    def test_dates_join_across_the_tags_footer_and_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._export(tmp)
            dates = m._simplenote_dates(d)
            tagged = m._note_norm(m._NOTE_TAGS_FOOTER.sub(
                "", (d / "Kort anteckning.txt").read_text(encoding="utf-8")))
            self.assertEqual(dates[tagged], ("2012-02-29", "2019-04-18"))
            plain = m._note_norm((d / "Utan tagg.txt").read_text(encoding="utf-8"))
            self.assertEqual(dates[plain], ("2020-07-17", "2020-07-17"))

    def test_trashed_notes_are_not_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = m._simplenote_dates(self._export(tmp))
            self.assertNotIn(m._note_norm("Slängd\n\nSka inte synas."), dates)

    def test_duplicate_content_resolves_to_the_oldest_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "source").mkdir(parents=True)
            (d / "source" / "notes.json").write_text(json.dumps({"activeNotes": [
                {"id": "y", "content": "Samma", "creationDate": "2021-05-05T00:00:00.000Z",
                 "lastModified": "2021-05-05T00:00:00.000Z"},
                {"id": "x", "content": "Samma", "creationDate": "2014-01-02T00:00:00.000Z",
                 "lastModified": "2014-01-02T00:00:00.000Z"},
            ]}), encoding="utf-8")
            self.assertEqual(m._simplenote_dates(d)[m._note_norm("Samma")][0], "2014-01-02")

    def test_missing_json_costs_the_dates_not_the_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(m._simplenote_dates(Path(tmp)), {})

    def test_real_export_joins_for_essentially_every_note(self):
        """The corpus assertion. A fixture pins the shape; only the real export
        pins reality — this is the check that would have caught a pattern that
        matched the fixture and nothing else."""
        d = Path(m.HOME) / ".claude/archives/simplenote-notes"
        files = sorted(d.glob("*.txt"))
        if not files or not (d / "source" / "notes.json").exists():
            self.skipTest("simplenote export not present on this machine")
        dates = m._simplenote_dates(d)
        dated = sum(
            1 for f in files
            if dates.get(m._note_norm(m._NOTE_TAGS_FOOTER.sub(
                "", f.read_text(encoding="utf-8", errors="replace"))))
        )
        self.assertGreaterEqual(dated / len(files), 0.99, f"only {dated}/{len(files)} joined")


class PartialKindPruneSafetyTests(unittest.TestCase):
    """The live-Gmail half made `email` a kind that can legitimately re-emit only
    PART of itself in a run: the 4.7 GB archives are skipped when unchanged, while
    the live half still runs. Without an exclusion the orphan-prune would read the
    un-emitted archive chunks as deleted and drop 658k of them."""

    def _run_ingest_recording_deletes(self, discoverers, kinds):
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

        args = type("A", (), {"sources": kinds})()
        with mock.patch.object(m, "load_conn", return_value=FakeConn()), \
             mock.patch.object(m, "ALL", discoverers):
            m.cmd_ingest(args)
        return [(s, p) for (s, p) in recorded
                if isinstance(s, str) and s.startswith("DELETE FROM chunks")]

    def setUp(self):
        self._saved = set(m._PARTIAL_KINDS)
        m._PARTIAL_KINDS.clear()

    def tearDown(self):
        m._PARTIAL_KINDS.clear()
        m._PARTIAL_KINDS.update(self._saved)

    def test_partial_kind_is_excluded_from_the_prune(self):
        def partial_email():
            m._PARTIAL_KINDS.add("email")
            yield ("email:new#0", "email", "email:new", 0, "gmail", "S", "b", "2026-08-13", "2026-08-13", "hh")

        def whole_doc():
            yield ("doc:one#0", "doc", "doc:one", 0, "proj", "T", "body", "", "", "hash")

        deletes = self._run_ingest_recording_deletes(
            {"email": partial_email, "doc": whole_doc}, ["email", "doc"])
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], (["doc"],), "email ran partially and must not be pruned")

    def test_a_whole_kind_is_still_pruned(self):
        def whole_email():
            yield ("email:new#0", "email", "email:new", 0, "gmail", "S", "b", "2026-08-13", "2026-08-13", "hh")

        deletes = self._run_ingest_recording_deletes({"email": whole_email}, ["email"])
        self.assertEqual(deletes[0][1], (["email"],))


class GmailLiveTests(unittest.TestCase):
    def test_watermark_advances_only_over_messages_that_came_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "gmail_state.json")
            fetched = {"a": (b"From: x\r\nSubject: A\r\n\r\nbody", 1_700_000_000_000),
                       "b": None}  # b fails to read
            with mock.patch.object(m, "_GMAIL_STATE", state), \
                 mock.patch.object(m, "_gmail_list_ids", return_value=["a", "b"]), \
                 mock.patch.object(m, "_gmail_fetch_raw", side_effect=lambda i: fetched[i]):
                list(m._gmail_live_messages())
            saved = json.load(open(state))
            self.assertEqual(saved["watermark_ms"], 1_700_000_000_000)
            self.assertEqual(saved["retry_ids"], ["b"], "a failed id must be retried, not silently dropped")

    def test_mbox_and_live_share_one_id_so_a_duplicate_upserts_onto_itself(self):
        raw = (b"Message-ID: <abc@example.com>\r\nSubject: Hej\r\n"
               b"From: a@b.se\r\nDate: Tue, 12 Aug 2026 10:00:00 +0200\r\n\r\nkropp")
        from_mbox = list(m._email_rows(b"From x\n" + raw, set()))
        from_live = list(m._email_rows(raw, set(), fallback_epoch_ms=1_760_000_000_000))
        self.assertTrue(from_mbox and from_live)
        self.assertEqual(from_mbox[0][0], from_live[0][0])
        self.assertEqual(from_mbox[0][7], "2026-08-12")

    def test_internaldate_fills_in_for_a_malformed_date_header(self):
        raw = b"Message-ID: <z@x>\r\nSubject: Trasig\r\nDate: not-a-date\r\n\r\nkropp"
        ms = 1_723_500_000_000
        rows = list(m._email_rows(raw, set(), fallback_epoch_ms=ms))
        # Expected value COMPUTED from the input, not typed from memory: my first
        # pass asserted 2024-08-13 by eyeballing the epoch and was off by a day.
        self.assertEqual(rows[0][7], time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)))
        self.assertEqual(rows[0][7], "2024-08-12")


class MalformedHeaderTests(unittest.TestCase):
    """A single malformed header aborted the 2026-08-13 Gmail backfill at 21%
    (26 000 of 123 338) with "address parts cannot contain CR or LF". The
    generator died and every remaining message went unfetched.

    The fixture below carries the REAL failure shape — a raw CR/LF folded into an
    address header, which `policy=default` only rejects when the header is
    stringified, not when the message is parsed. An invented "weird header" would
    not reproduce it, because the exception comes from address parsing
    specifically."""

    RAW = (b"Message-ID: <ok@example.com>\r\n"
           b"Subject: Fungerar\r\n"
           b"From: \"Broken\r\n Name\" <a@b.se>, <c@\r\nd.se>\r\n"
           b"To: x@y.se\r\n"
           b"Date: Tue, 12 Aug 2026 10:00:00 +0200\r\n\r\nkropp")

    def test_a_malformed_address_header_does_not_kill_the_message(self):
        rows = list(m._email_rows(self.RAW, set()))
        self.assertTrue(rows, "the message must still be indexed")
        self.assertEqual(rows[0][5], "Fungerar", "the good headers still land")

    def test_internaldate_still_dates_it_when_the_headers_are_mangled(self):
        # A raw CR/LF can break the header BOUNDARIES, so Date: may be
        # unrecoverable no matter how defensively it is read. That is exactly why
        # the live path passes Gmail's own internalDate as a fallback — assert the
        # guarantee the fix actually makes, not a date this hand-built fixture
        # happens to preserve.
        ms = 1_723_500_000_000
        rows = list(m._email_rows(self.RAW, set(), fallback_epoch_ms=ms))
        self.assertEqual(rows[0][7], time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)))

    def test_the_message_is_keyed_and_deduped_normally(self):
        seen = set()
        first = list(m._email_rows(self.RAW, seen))
        second = list(m._email_rows(self.RAW, seen))
        self.assertTrue(first)
        self.assertEqual(second, [], "same Message-ID must dedup on the second pass")
