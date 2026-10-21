"""A screenshot deleted from Photos keeps its text in the index, read from the VOLTA archive.

The nightly ingest walks the Photos library, and its orphan prune deletes every
screenshot row the walk did not emit. Deleting screenshots from Photos to free
iCloud space would therefore delete their text from mannaminne the next night,
although their originals and their Photos records sit on VOLTA. These tests pin
that a screenshot absent from the library is still emitted from the archive, with
the same id and body it had while it was in the library, so nothing is pruned and
nothing is embedded again. Background: dotfiles docs/photos_archive_offload_2026-07-28.md.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import mannaminne as mm  # noqa: E402

TZ = timezone(timedelta(hours=1))


def _info(uuid, name, date, labels):
    return {"uuid": uuid, "original_filename": name, "date": date.isoformat(),
            "labels": labels, "screenshot": True}


class _Photo:
    def __init__(self, uuid, name, date, labels):
        self.uuid, self.original_filename, self.date = uuid, name, date
        self.labels, self.screenshot, self.path = labels, True, None


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A VOLTA folder with two exported screenshots, A and B, their export records,
    and an OCR cache that already holds their text, so no OCR runs."""
    volta = tmp_path / "VOLTA"
    volta.mkdir()
    da = datetime(2024, 1, 2, 10, 0, tzinfo=TZ)
    db_ = datetime(2023, 5, 6, 22, 30, tzinfo=TZ)
    shots = {"A": ("IMG_0001.PNG", da, ["Text", "Document"]),
             "B": ("IMG_0002.PNG", db_, ["Screenshot"])}
    con = sqlite3.connect(volta / ".osxphotos_export.db")
    con.execute("CREATE TABLE export_data (uuid TEXT, filepath TEXT, error TEXT)")
    con.execute("CREATE TABLE photoinfo (uuid TEXT, photoinfo TEXT)")
    cache = {}
    for uuid, (name, date, labels) in shots.items():
        (volta / name).write_bytes(b"png")
        con.execute("INSERT INTO export_data VALUES (?, ?, NULL)", (uuid, name))
        con.execute("INSERT INTO photoinfo VALUES (?, ?)",
                    (uuid, json.dumps(_info(uuid, name, date, labels))))
        cache[str(volta / name)] = f"text of {uuid}"
    con.commit()
    con.close()
    ocr = tmp_path / "ocr_cache.json"
    ocr.write_text(json.dumps(cache))
    fermi = tmp_path / "fermi-ss"
    fermi.mkdir()
    monkeypatch.setattr(mm, "_VOLTA_SS", str(volta))
    monkeypatch.setattr(mm, "_VOLTA_ORIG_CACHE", None)
    monkeypatch.setattr(mm, "_OCR_CACHE", ocr)
    monkeypatch.setattr(mm, "_FERMI_SS", str(fermi))
    monkeypatch.delenv("MANNAMINNE_SS_LIMIT", raising=False)
    return shots


def _library(monkeypatch, photos):
    fake = types.ModuleType("osxphotos")
    fake.PhotosDB = lambda *_a, **_k: types.SimpleNamespace(photos=lambda: photos)
    monkeypatch.setitem(sys.modules, "osxphotos", fake)


def _rows_by_source():
    out = {}
    for row in mm.discover_screenshots():
        out.setdefault(row[2], []).append(row)
    return out


def test_a_screenshot_deleted_from_photos_is_still_emitted_from_the_archive(archive, monkeypatch):
    name, date, labels = archive["A"]
    _library(monkeypatch, [_Photo("A", name, date, labels)])  # B has left the library
    rows = _rows_by_source()
    assert set(rows) == {"photo:A", "photo:B"}


def test_the_archive_row_is_identical_to_the_library_row(archive, monkeypatch):
    """Same id, body, date and project as while the photo was in Photos, so the
    upsert changes nothing and the embedding is reused."""
    name, date, labels = archive["B"]
    _library(monkeypatch, [_Photo("B", name, date, labels)])
    in_library = _rows_by_source()["photo:B"]
    mm._VOLTA_ORIG_CACHE = None
    _library(monkeypatch, [])
    from_archive = _rows_by_source()["photo:B"]
    assert from_archive == in_library


def test_a_photo_in_the_library_is_emitted_once(archive, monkeypatch):
    _library(monkeypatch, [_Photo(u, *archive[u]) for u in ("A", "B")])
    rows = _rows_by_source()
    assert len(rows["photo:A"]) == 1 and len(rows["photo:B"]) == 1


def test_an_unmounted_volta_skips_the_kind_instead_of_emitting_less(archive, monkeypatch):
    """Without VOLTA the walk would emit screenshots without text, and the left-library
    ones not at all; the prune would then delete them. Skipping the kind keeps them."""
    monkeypatch.setattr(mm, "_volume_available", lambda p: p != mm._VOLTA_SS)
    _library(monkeypatch, [])
    with pytest.raises(mm.SourceUnavailable):
        _rows_by_source()


def test_an_unreadable_export_db_skips_the_kind(archive, monkeypatch):
    (Path(mm._VOLTA_SS) / ".osxphotos_export.db").unlink()
    _library(monkeypatch, [])
    with pytest.raises(mm.SourceUnavailable):
        _rows_by_source()


def test_an_archive_file_without_its_photos_record_is_skipped(archive, monkeypatch):
    con = sqlite3.connect(Path(mm._VOLTA_SS) / ".osxphotos_export.db")
    con.execute("DELETE FROM photoinfo WHERE uuid = 'B'")
    con.commit()
    con.close()
    _library(monkeypatch, [])
    assert set(_rows_by_source()) == {"photo:A"}
