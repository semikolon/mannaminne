#!/usr/bin/env python3
"""Index the personal Gmail from before the live floor, once.

The live half of the email source starts at GMAIL_SINCE_FLOOR (2014-09-04), on
the belief that the 2014 Takeout covered what came before. That Takeout is the
work mailbox, so personal mail before the floor was in neither half (found
2026-10-17: 117 of the 177,316 indexed messages before it are personal).

This lists every message before the floor, takes its original from a
`gmail_sweep` copy where one exists and from Gmail otherwise, paced under the
quota (100 raw messages a minute per user), and writes it through the ingest's
own parser and upsert, so a message already indexed simply upserts onto itself.

Resumable: finished ids are appended to ~/.cache/mannaminne/gmail_backfill_done.txt.
Refuses to write while a table rebuild is in progress (a `chunks_part` table exists).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_sweep as gs  # noqa: E402
import inbox_messages as im  # noqa: E402

BEFORE = os.environ.get("MANNAMINNE_BACKFILL_BEFORE", "2014/09/05")
DONE = Path.home() / ".cache/mannaminne/gmail_backfill_done.txt"
BATCH = 200


def list_before(before):
    pages = im._gws(["gmail", "users", "messages", "list", "--params",
                     json.dumps({"userId": "me", "maxResults": 500, "q": f"before:{before} -in:spam"}),
                     "--page-all", "--page-limit", "5000", "--page-delay", "0"])
    return [m["id"] for line in pages for m in json.loads(line).get("messages", [])]


def originals(ids, copies, sweep_dir, fetch=gs.fetch_raw, pace=None, workers=gs.WORKERS):
    """Yield (id, raw-bytes or None) in order. A sweep copy is read from disk;
    anything else is fetched. None means gone or unreadable after retries."""
    pace = pace or gs.Pace(gs.COPY_PER_MIN)
    stop = threading.Event()

    def get(mid):
        if mid in copies:
            return mid, (sweep_dir / copies[mid]["path"]).read_bytes()
        errors = waits = 0
        while not stop.is_set():
            pace.wait()
            res = fetch(mid)
            if res[0] == "ok":
                return mid, res[1]
            if res[0] == "rate":
                pace.back_off()
                waits += 1
                if waits > 60:
                    return mid, None
                continue
            if res[0] == "gone":
                return mid, None
            if res[0] == "auth":
                stop.set()
                raise RuntimeError(f"Gmail refused the login: {res[1]}")
            errors += 1
            if errors >= 4:
                return mid, None
            time.sleep(2 ** errors)
        return mid, None

    with ThreadPoolExecutor(workers) as ex:
        yield from ex.map(get, ids)


def rebuild_in_progress(cur):
    cur.execute("SELECT to_regclass('chunks_part') IS NOT NULL")
    return cur.fetchone()[0]


def backfill(ids, conn, copies, sweep_dir, done_path=DONE, log=print, **kw):
    import mannaminne
    done = set(gs.load_ids(done_path))
    todo = [m for m in ids if m not in done]
    log(f"backfill: {len(ids)} messages before the floor, {len(todo)} left, "
        f"{sum(1 for m in todo if m in copies)} of them from sweep copies")
    seen, rows, batch_ids, written, unreadable = set(), [], [], 0, 0
    with conn.cursor() as cur, open(done_path, "a", encoding="utf-8") as fh:
        if rebuild_in_progress(cur):
            raise RuntimeError("a table rebuild is in progress (chunks_part exists); not writing")

        def flush():
            nonlocal rows, batch_ids, written
            if rows:
                mannaminne._upsert(cur, rows)
            conn.commit()
            fh.write("".join(f"{m}\n" for m in batch_ids))
            fh.flush()
            written += len(batch_ids)
            rows, batch_ids = [], []

        for mid, raw in originals(todo, copies, sweep_dir, **kw):
            if raw is None:
                unreadable += 1
            else:
                rows += list(mannaminne._email_rows(raw, seen))
            batch_ids.append(mid)
            if len(batch_ids) >= BATCH:
                flush()
                log(f"  {written}/{len(todo)}")
        flush()
        mannaminne._ensure_email_class(conn, cur)
        conn.commit()
    log(f"backfill finished: {written} messages processed, {unreadable} unreadable")
    return written, unreadable


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", default=BEFORE)
    args = ap.parse_args(argv)
    import mannaminne
    ids = list_before(args.before)
    copies = gs.load_copied(gs.DEST) if gs.DEST.exists() else {}
    with mannaminne.load_conn() as conn:
        backfill(ids, conn, copies, gs.DEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
