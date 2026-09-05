"""Per-file incremental ingest, and the carry-forward that makes it safe.

The optimisation skips files that have not changed since the last completed run.
The hazard it creates is precise: `cmd_ingest`'s orphan prune deletes chunks of a
completed kind that were not emitted this run, so a skipped file's chunks must be
marked seen or skipping would DELETE the index it was meant to preserve. These
tests pin both halves, and pin that a failure in the optimisation keeps too much
rather than too little. Background: docs/nightly_ingest_cost_2026-09-05.md.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import mannaminne as mm  # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Every test gets its own fingerprint store and empty in-memory state; nothing
    reads or writes the real ~/.cache/mannaminne."""
    monkeypatch.setattr(mm, "_FILE_FP_DIR", str(tmp_path / "fps"))
    mm._PENDING_FILE_FPS.clear()
    mm._CARRIED_SOURCES.clear()
    mm._CARRIED_PROJECTS.clear()
    mm._unchanged.__defaults__[-1].clear()  # the _fps_cache default dict
    yield
    mm._PENDING_FILE_FPS.clear()
    mm._CARRIED_SOURCES.clear()
    mm._CARRIED_PROJECTS.clear()
    mm._unchanged.__defaults__[-1].clear()


def _touch(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestFingerprint:
    def test_missing_file_has_no_fingerprint(self, tmp_path):
        assert mm._path_fp(tmp_path / "nope") is None

    def test_fingerprint_changes_with_content(self, tmp_path):
        f = _touch(tmp_path / "a.md", "one")
        first = mm._path_fp(f)
        time.sleep(0.01)
        f.write_text("two but longer", encoding="utf-8")
        assert mm._path_fp(f) != first

    def test_fingerprint_survives_a_read(self, tmp_path):
        f = _touch(tmp_path / "a.md", "one")
        before = mm._path_fp(f)
        f.read_text()
        assert mm._path_fp(f) == before  # atime must not be part of it


class TestUnchanged:
    def test_first_sight_is_changed_then_unchanged_after_save(self, tmp_path):
        f = _touch(tmp_path / "a.md", "hello")
        assert mm._unchanged("doc", f, source_id="doc:x:a.md") is False
        mm._save_file_fps("doc")
        mm._unchanged.__defaults__[-1].clear()  # next run: cold cache
        assert mm._unchanged("doc", f, source_id="doc:x:a.md") is True

    def test_a_touched_file_is_changed_again(self, tmp_path):
        f = _touch(tmp_path / "a.md", "hello")
        mm._unchanged("doc", f, source_id="doc:x:a.md")
        mm._save_file_fps("doc")
        mm._unchanged.__defaults__[-1].clear()
        time.sleep(0.01)
        f.write_text("hello, more", encoding="utf-8")
        assert mm._unchanged("doc", f, source_id="doc:x:a.md") is False

    def test_every_path_asked_about_is_recorded(self, tmp_path):
        a = _touch(tmp_path / "a.md", "a")
        b = _touch(tmp_path / "b.md", "b")
        mm._unchanged("doc", a, source_id="doc:x:a.md")
        mm._unchanged("doc", b, source_id="doc:x:b.md")
        assert set(mm._PENDING_FILE_FPS["doc"]) == {str(a), str(b)}

    def test_a_vanished_file_drops_out_of_the_map(self, tmp_path):
        a = _touch(tmp_path / "a.md", "a")
        b = _touch(tmp_path / "b.md", "b")
        mm._unchanged("doc", a, source_id="doc:x:a.md")
        mm._unchanged("doc", b, source_id="doc:x:b.md")
        mm._save_file_fps("doc")
        # next run: b is gone, so it is neither recorded nor carried, and its chunks
        # prune normally — which is how a deleted document leaves the index.
        mm._PENDING_FILE_FPS.clear()
        mm._CARRIED_SOURCES.clear()
        mm._unchanged.__defaults__[-1].clear()
        b.unlink()
        assert mm._unchanged("doc", a, source_id="doc:x:a.md") is True
        assert mm._unchanged("doc", b, source_id="doc:x:b.md") is False
        assert mm._CARRIED_SOURCES["doc"] == {"doc:x:a.md"}
        mm._save_file_fps("doc")
        assert set(mm._load_file_fps("doc")) == {str(a)}

    def test_only_a_skip_carries_the_source(self, tmp_path):
        f = _touch(tmp_path / "a.md", "hello")
        mm._unchanged("doc", f, source_id="doc:x:a.md")
        assert mm._CARRIED_SOURCES.get("doc", set()) == set()  # changed: nothing carried
        mm._save_file_fps("doc")
        mm._unchanged.__defaults__[-1].clear()
        mm._unchanged("doc", f, source_id="doc:x:a.md")
        assert mm._CARRIED_SOURCES["doc"] == {"doc:x:a.md"}

    def test_projects_are_carried_separately(self, tmp_path):
        f = _touch(tmp_path / "repo/.git/logs/HEAD", "sha ref")
        mm._unchanged("git_commit", f, project="repo")
        mm._save_file_fps("git_commit")
        mm._unchanged.__defaults__[-1].clear()
        assert mm._unchanged("git_commit", f, project="repo") is True
        assert mm._CARRIED_PROJECTS["git_commit"] == {"repo"}
        assert mm._CARRIED_SOURCES.get("git_commit", set()) == set()

    def test_fingerprints_are_not_saved_when_the_kind_did_not_complete(self, tmp_path):
        """cmd_ingest calls _save_file_fps only after a kind ingests to completion.
        Without that call the next run must re-ingest, never falsely skip."""
        f = _touch(tmp_path / "a.md", "hello")
        mm._unchanged("doc", f, source_id="doc:x:a.md")
        # no _save_file_fps here — simulating a mid-kind crash
        mm._unchanged.__defaults__[-1].clear()
        mm._PENDING_FILE_FPS.clear()
        assert mm._unchanged("doc", f, source_id="doc:x:a.md") is False


class FakeCopy:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def write_row(self, row):
        self.sink.append(row[0])


class FakeCursor:
    """Records SQL. `fail_on` makes the matching statement raise, so the
    carry-failure path can be exercised."""

    def __init__(self, fail_on: str | None = None):
        self.sql: list[tuple[str, tuple]] = []
        self.copied: list[str] = []
        self.upserted: list[tuple] = []
        self.fail_on = fail_on
        self.rowcount = 0

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        self.sql.append((" ".join(sql.split()), params or ()))
        self.rowcount = 0

    def executemany(self, sql, rows):
        self.upserted.extend(rows)

    def copy(self, sql):
        return FakeCopy(self.copied)


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _run_ingest(monkeypatch, cur, rows_by_kind, carried=None, carried_projects=None):
    conn = FakeConn(cur)
    monkeypatch.setattr(mm, "load_conn", lambda: conn)
    monkeypatch.setattr(mm, "ALL", {k: (lambda rs=rs: iter(rs)) for k, rs in rows_by_kind.items()})
    mm._CARRIED_SOURCES.clear()
    mm._CARRIED_SOURCES.update({k: set(v) for k, v in (carried or {}).items()})
    mm._CARRIED_PROJECTS.clear()
    mm._CARRIED_PROJECTS.update({k: set(v) for k, v in (carried_projects or {}).items()})

    class Args:
        sources = list(rows_by_kind)

    mm.cmd_ingest(Args())
    return conn


def _row(chunk_id, kind, source_id):
    return (chunk_id, kind, source_id, 0, "p", "t", "text", "2026-01-01", "2026-01-01", "hash")


class TestCarryForward:
    def test_skipped_sources_are_marked_seen_before_the_prune(self, monkeypatch):
        cur = FakeCursor()
        _run_ingest(
            monkeypatch,
            cur,
            {"doc": [_row("doc:x:new.md#0", "doc", "doc:x:new.md")]},
            carried={"doc": ["doc:x:old.md"]},
        )
        inserts = [s for s, _ in cur.sql if s.startswith("INSERT INTO _seen")]
        assert inserts, "the skipped source's chunks were never marked seen"
        assert any(p == ("doc", ["doc:x:old.md"]) for s, p in cur.sql if "INSERT INTO _seen" in s)
        # and the prune still runs for the kind
        assert any("DELETE FROM chunks" in s for s, _ in cur.sql)

    def test_the_carry_precedes_the_delete(self, monkeypatch):
        cur = FakeCursor()
        _run_ingest(
            monkeypatch,
            cur,
            {"doc": [_row("doc:x:new.md#0", "doc", "doc:x:new.md")]},
            carried={"doc": ["doc:x:old.md"]},
        )
        order = [i for i, (s, _) in enumerate(cur.sql) if s.startswith(("INSERT INTO _seen", "DELETE FROM chunks"))]
        first = cur.sql[order[0]][0]
        assert first.startswith("INSERT INTO _seen")

    def test_carried_projects_use_the_project_column(self, monkeypatch):
        cur = FakeCursor()
        _run_ingest(
            monkeypatch,
            cur,
            {"git_commit": [_row("gitcommit:r:sha#0", "git_commit", "gitcommit:r:sha")]},
            carried_projects={"git_commit": ["otherrepo"]},
        )
        assert any(
            "INSERT INTO _seen" in s and "project = ANY" in s and p == ("git_commit", ["otherrepo"])
            for s, p in cur.sql
        )

    def test_a_failed_carry_drops_the_kind_from_the_prune(self, monkeypatch):
        """The failure mode must be 'keeps too much', never 'deletes'."""
        cur = FakeCursor(fail_on="INSERT INTO _seen")
        conn = _run_ingest(
            monkeypatch,
            cur,
            {"doc": [_row("doc:x:new.md#0", "doc", "doc:x:new.md")]},
            carried={"doc": ["doc:x:old.md"]},
        )
        deletes = [(q, p) for q, p in cur.sql if q.startswith("DELETE FROM chunks")]
        assert not any(p and "doc" in (p[0] or []) for _q, p in deletes), (
            "doc was pruned although its carry-forward failed"
        )
        assert conn.rollbacks >= 1

    def test_no_skips_means_no_carry_sql_at_all(self, monkeypatch):
        cur = FakeCursor()
        _run_ingest(monkeypatch, cur, {"doc": [_row("doc:x:a.md#0", "doc", "doc:x:a.md")]})
        assert not [s for s, _ in cur.sql if s.startswith("INSERT INTO _seen")]
        assert any("DELETE FROM chunks" in s for s, _ in cur.sql)


class TestDiscovererIntegration:
    """The real discoverer against real files: second run does no work and carries."""

    def test_docs_are_read_once_and_carried_the_second_time(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "dotfiles" / "docs").mkdir(parents=True)
        (home / "dotfiles" / "docs" / "one.md").write_text("# One\n\nbody\n", encoding="utf-8")
        (home / "dotfiles" / "docs" / "two.md").write_text("# Two\n\nbody\n", encoding="utf-8")
        (home / "Documents").mkdir()
        (home / "Library/Mobile Documents/com~apple~CloudDocs").mkdir(parents=True)
        (home / "Projects").mkdir()
        monkeypatch.setattr(mm, "HOME", str(home))

        first = list(mm.discover_docs())
        ids = {r[2] for r in first}
        assert len(ids) >= 2, ids
        assert mm._CARRIED_SOURCES.get("doc", set()) == set()
        mm._save_file_fps("doc")

        mm._PENDING_FILE_FPS.clear()
        mm._CARRIED_SOURCES.clear()
        mm._unchanged.__defaults__[-1].clear()
        second = list(mm.discover_docs())
        assert second == [] or {r[2] for r in second}.isdisjoint(ids), (
            "unchanged documents were re-emitted"
        )
        assert ids <= mm._CARRIED_SOURCES["doc"], "unchanged documents were not carried"

    def test_a_changed_document_is_re_emitted_while_the_others_are_carried(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        (home / "dotfiles" / "docs").mkdir(parents=True)
        a = home / "dotfiles" / "docs" / "one.md"
        b = home / "dotfiles" / "docs" / "two.md"
        a.write_text("# One\n\nbody\n", encoding="utf-8")
        b.write_text("# Two\n\nbody\n", encoding="utf-8")
        (home / "Documents").mkdir()
        (home / "Library/Mobile Documents/com~apple~CloudDocs").mkdir(parents=True)
        (home / "Projects").mkdir()
        monkeypatch.setattr(mm, "HOME", str(home))

        list(mm.discover_docs())
        mm._save_file_fps("doc")
        mm._PENDING_FILE_FPS.clear()
        mm._CARRIED_SOURCES.clear()
        mm._unchanged.__defaults__[-1].clear()

        time.sleep(0.01)
        a.write_text("# One\n\nbody, edited today\n", encoding="utf-8")
        rows = list(mm.discover_docs())
        emitted = {r[2] for r in rows}
        assert any("one.md" in s for s in emitted), "the edited document was not re-read"
        assert not any("two.md" in s for s in emitted), "an untouched document was re-read"
        assert any("two.md" in s for s in mm._CARRIED_SOURCES["doc"])
