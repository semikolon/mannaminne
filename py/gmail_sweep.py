#!/usr/bin/env python3
"""Move the swept bulk mail out of Gmail without losing any of it.

The sweep is `inbox_messages.in_sweep`: mail in updates, promotions or social
that was never opened and that nothing protects. Fredrik ruled on 2026-10-17
("Go!") to carry out the plan in
`~/dotfiles/docs/gmail_storage_and_index_pruning_2026-09-06.md`: copy every
message in original form to FERMI, make sure the index holds each one, then
move them to Gmail's trash, which can be undone for 30 days.

  plan           fresh inventory. The plan is the 2026-10-08 sweep, minus every
                 message that no longer qualifies (opened, starred, labelled,
                 replied to since) and every message his documents cite
  copy           fetch each planned message raw and write it as an .eml file;
                 resumable, safe to stop and start
  check          every planned message copied intact, which ones the index
                 lacks, which ones his documents cite by index id
  index-missing  add the messages the index lacks, from the copies
  trash          move the planned, copied, indexed messages to Gmail's trash,
                 only once a backup snapshot holds every copy
  untrash        undo the trash step, within Gmail's 30 days
  status         where the run stands

Speed is set by Gmail, not by this program. Gmail allows 6,000 quota units per
user per minute and a raw message costs 60, so at most 100 messages a minute
however they are requested: batching saves HTTP round trips, not quota.
Measured 2026-10-17: 117 fetched in 19 seconds, then rateLimitExceeded. The
copy runs below that line and backs off when it is hit.

Nothing here deletes anything permanently. Trash is a label; `untrash` removes it.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_messages as im  # noqa: E402

DEST = Path(os.environ.get("GMAIL_SWEEP_DIR", "/Volumes/FERMI/Backup/gmail-sweep-2026-10"))
#: The inventory the plan was approved on. Mail that arrived since is not in it,
#: so nothing he has not seen counted is swept.
APPROVED_TABLE = im.CACHE / "table_2026-10-08.json"
COPY_PER_MIN = int(os.environ.get("GMAIL_SWEEP_PER_MIN", "90"))
WORKERS = 8
RATE_PAUSE_S = 30
#: Folders whose documents may cite a message by Gmail id or index id. A cited
#: message stays in Gmail, so a link to it keeps working. The list is personal
#: and lives outside the repo, one folder per line.
CITED_ROOTS_FILE = Path.home() / ".config/mannaminne/sweep_cited_roots.txt"


def cited_roots():
    try:
        lines = CITED_ROOTS_FILE.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    return [Path(os.path.expanduser(l.strip())) for l in lines
            if l.strip() and not l.startswith("#")] or [Path.home() / "dotfiles/docs"]


#: Where the outcome is pushed: an ntfy topic URL, from the environment so no
#: personal address sits in the repo. Unset, nothing is pushed.
NTFY_URL = os.environ.get("GMAIL_SWEEP_NTFY_URL", "")
GMAIL_ID = re.compile(r"\b[0-9a-f]{16}\b")
INDEX_ID = re.compile(r"\bemail:([0-9a-f]{16})\b")
TRASH_BATCH = 500


# --- files ------------------------------------------------------------------

def _lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [l.rstrip("\n") for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def load_ids(path):
    return [l.split("\t")[0] for l in _lines(path)]


def load_copied(dest):
    """id -> record, for every message whose copy was written and recorded."""
    out = {}
    for l in _lines(dest / "copied.tsv"):
        mid, internal, size, sha, src, rel = l.split("\t")
        out[mid] = {"internal": int(internal), "bytes": int(size), "sha256": sha,
                    "source_id": src, "path": rel}
    return out


class Appender:
    """One tab-separated record per line, appended under a lock and flushed at
    once, so a stopped run resumes from what is on disk."""

    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()

    def add(self, *fields):
        with self.lock:
            self.fh.write("\t".join(str(f) for f in fields) + "\n")
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()


# --- plan -------------------------------------------------------------------

def sweep_ids(table, patterns):
    return {m for m, r in table.items() if im.in_sweep(r, patterns)}


def cited_ids(roots=None):
    """Gmail ids and index ids that appear anywhere in his documents."""
    gmail, index = set(), set()
    for root in roots if roots is not None else cited_roots():
        if not root.exists():
            continue
        p = subprocess.run(["rg", "-o", "--no-filename", "-g", "*.md", "-g", "*.txt",
                            "-g", "*.json", "-g", "*.tsv", r"\b[0-9a-f]{16}\b|email:[0-9a-f]{16}",
                            str(root)], capture_output=True, text=True)
        for tok in p.stdout.split():
            m = INDEX_ID.fullmatch(tok)
            if m:
                index.add(m.group(1))
            elif GMAIL_ID.fullmatch(tok):
                gmail.add(tok)
    return gmail, index


def make_plan(approved_table, fresh_table, patterns, cited_gmail):
    approved = sweep_ids(approved_table, patterns)
    now = sweep_ids(fresh_table, patterns)
    gone = {m for m in approved if m not in fresh_table}
    changed = {m for m in approved if m in fresh_table and m not in now}
    cited = approved & cited_gmail
    plan = sorted((approved & now) - cited_gmail)
    return plan, {"approved_2026_10_08": len(approved), "planned": len(plan),
                  "gone_from_gmail": len(gone), "no_longer_in_sweep": len(changed),
                  "cited_in_docs": len(cited)}


def cmd_plan(args):
    DEST.mkdir(parents=True, exist_ok=True)
    approved = json.loads(APPROVED_TABLE.read_text())
    fresh = im.build(fresh=True)
    gmail, _ = cited_ids()
    plan, summary = make_plan(approved, fresh, im.bulk_label_patterns(), gmail)
    (DEST / "plan.tsv").write_text("".join(f"{m}\n" for m in plan))
    summary["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    (DEST / "plan.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


# --- copy -------------------------------------------------------------------

class Pace:
    """At most `per_min` fetches start per minute across all workers, and one
    shared pause when Gmail says the minute's quota is spent."""

    def __init__(self, per_min, clock=time.monotonic, sleep=time.sleep):
        self.gap = 60.0 / per_min
        self.clock, self.sleep = clock, sleep
        self.next = 0.0
        self.pause_until = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            start = max(self.clock(), self.next, self.pause_until)
            self.next = start + self.gap
        delay = start - self.clock()
        if delay > 0:
            self.sleep(delay)

    def back_off(self, seconds=RATE_PAUSE_S):
        with self.lock:
            self.pause_until = max(self.pause_until, self.clock() + seconds)


def classify(returncode, stdout):
    """What one gws answer means: ('ok', raw-bytes, internal-ms), ('rate',),
    ('gone',), ('auth', why) or ('error', why)."""
    try:
        d = json.loads(stdout[stdout.index("{"):])
    except ValueError:
        return ("error", stdout[-200:])
    if returncode == 0 and d.get("raw"):
        return ("ok", base64.urlsafe_b64decode(d["raw"].encode()), int(d.get("internalDate") or 0))
    err = d.get("error") or {}
    code, reason = err.get("code"), str(err.get("reason", "")) + str(err.get("status", ""))
    if code == 429 or "ateLimit" in reason or "RESOURCE_EXHAUSTED" in reason:
        return ("rate",)
    if code == 404:
        return ("gone",)
    if code == 401:
        return ("auth", err.get("message", ""))
    return ("error", str(err.get("message", stdout[-200:])))


def fetch_raw(mid, run=subprocess.run):
    p = run(["gws", "gmail", "users", "messages", "get", "--params",
             json.dumps({"userId": "me", "id": mid, "format": "raw"})],
            capture_output=True, text=True, timeout=120)
    return classify(p.returncode, p.stdout)


def index_source_id(raw):
    """The id the index gives this message, from the same parser the ingest
    uses; '-' when the message has neither subject nor body and is not indexed."""
    import mannaminne
    for row in mannaminne._email_rows(raw, set()):
        return row[2]
    return "-"


def eml_path(dest, mid, internal_ms):
    month = (datetime.datetime.fromtimestamp(internal_ms / 1000, datetime.timezone.utc)
             .strftime("%Y-%m") if internal_ms else "undated")
    return Path("eml") / month / f"{mid}.eml"


def copy(plan, dest, fetch=fetch_raw, pace=None, workers=WORKERS, log=print,
         source_id=index_source_id, max_rate_waits=60, max_errors=4, stop_after_failures=50):
    """Copy every planned message not yet copied. Returns (exit code, why)."""
    pace = pace or Pace(COPY_PER_MIN)
    done = load_copied(dest)
    skip = set(done) | set(load_ids(dest / "gone.tsv"))
    todo = [m for m in plan if m not in skip]
    copied, gone, failed = (Appender(dest / f) for f in ("copied.tsv", "gone.tsv", "failed.tsv"))
    stop, why = threading.Event(), []
    state = {"ok": 0, "run_of_failures": 0}
    lock = threading.Lock()
    t0 = time.time()
    log(f"copy: {len(todo)} to fetch, {len(done)} already copied")

    def halt(code, reason):
        with lock:
            if not why:
                why.append((code, reason))
        stop.set()

    def work(mid):
        rate_waits = errors = 0
        while not stop.is_set():
            pace.wait()
            res = fetch(mid)
            kind = res[0]
            if kind == "ok":
                raw, internal = res[1], res[2]
                rel = eml_path(dest, mid, internal)
                path = dest / rel
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(raw)
                    os.replace(tmp, path)
                except OSError as e:
                    halt(2, f"cannot write to {dest}: {e}")
                    return
                copied.add(mid, internal, len(raw), hashlib.sha256(raw).hexdigest(),
                           source_id(raw), rel)
                with lock:
                    state["ok"] += 1
                    state["run_of_failures"] = 0
                    n = state["ok"]
                if n % 500 == 0:
                    rate = n / (time.time() - t0) * 60
                    left = (len(todo) - n) / rate if rate else 0
                    log(f"  {n}/{len(todo)} copied, {rate:.0f}/min, about {left / 60:.1f} h left")
                return
            if kind == "rate":
                pace.back_off()
                rate_waits += 1
                if rate_waits > max_rate_waits:
                    failed.add(mid, "rate limit did not lift")
                    return
                continue
            if kind == "gone":
                gone.add(mid)
                return
            if kind == "auth":
                halt(3, f"Gmail refused the login: {res[1]}")
                return
            errors += 1
            if errors >= max_errors:
                failed.add(mid, res[1].replace("\t", " ").replace("\n", " ")[:200])
                with lock:
                    state["run_of_failures"] += 1
                    runaway = state["run_of_failures"] >= stop_after_failures
                if runaway:
                    halt(4, f"{stop_after_failures} messages failed in a row; last: {res[1][:200]}")
                return
            time.sleep(2 ** errors)

    try:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, todo))
    finally:
        for a in (copied, gone, failed):
            a.close()
    if why:
        log(f"copy stopped: {why[0][1]}")
        return why[0]
    log(f"copy finished: {state['ok']} copied this run in {(time.time() - t0) / 3600:.1f} h")
    return (0, "done")


def cmd_copy(args):
    if not DEST.exists():
        print(f"{DEST} is missing (is FERMI mounted?)", file=sys.stderr)
        return 2
    pidfile = DEST / "copy.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 0)
            print(f"a copy is already running (pid {pidfile.read_text().strip()})", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            pass
    pidfile.write_text(str(os.getpid()))
    try:
        code, _ = copy(load_ids(DEST / "plan.tsv"), DEST, workers=args.workers)
        return code
    finally:
        pidfile.unlink(missing_ok=True)


# --- check ------------------------------------------------------------------

def present_in_index(source_ids, conn):
    found = set()
    ids = sorted(source_ids)
    with conn.cursor() as cur:
        for i in range(0, len(ids), 1000):
            cur.execute("SELECT DISTINCT source_id FROM chunks WHERE source_kind = 'email' "
                        "AND source_id = ANY(%s)", (ids[i:i + 1000],))
            found.update(r[0] for r in cur.fetchall())
    return found


def check(dest, plan, conn, cited_index, verify_bytes=True):
    copied = load_copied(dest)
    gone = set(load_ids(dest / "gone.tsv"))
    failed = set(load_ids(dest / "failed.tsv"))
    not_copied = [m for m in plan if m not in copied and m not in gone]
    damaged = []
    if verify_bytes:
        for m in plan:
            rec = copied.get(m)
            if not rec:
                continue
            try:
                data = (dest / rec["path"]).read_bytes()
            except OSError:
                damaged.append(m)
                continue
            if hashlib.sha256(data).hexdigest() != rec["sha256"]:
                damaged.append(m)
    srcs = {copied[m]["source_id"] for m in plan if m in copied} - {"-"}
    present = present_in_index(srcs, conn) if srcs else set()
    indexed = present | {"-"}
    missing = [m for m in plan if m in copied and copied[m]["source_id"] not in indexed]
    spare = [m for m in plan if m in copied
             and copied[m]["source_id"].removeprefix("email:") in cited_index]
    (dest / "missing.tsv").write_text("".join(f"{m}\t{copied[m]['source_id']}\n" for m in missing))
    (dest / "spare.tsv").write_text("".join(f"{m}\n" for m in spare))
    summary = {"planned": len(plan), "copied": sum(1 for m in plan if m in copied),
               "gone": len(gone), "failed": len(failed - set(copied)),
               "not_copied": len(not_copied), "damaged": len(damaged),
               "missing_from_index": len(missing), "cited_by_index_id": len(spare),
               "bytes": sum(copied[m]["bytes"] for m in plan if m in copied),
               "at": datetime.datetime.now().isoformat(timespec="seconds")}
    (dest / "check.json").write_text(json.dumps(summary, indent=1))
    return summary


def cmd_check(args):
    import mannaminne
    _, cited_index = cited_ids()
    with mannaminne.load_conn() as conn:
        print(json.dumps(check(DEST, load_ids(DEST / "plan.tsv"), conn, cited_index), indent=1))


def cmd_index_missing(args):
    import mannaminne
    rows = []
    copied = load_copied(DEST)
    for mid in load_ids(DEST / "missing.tsv"):
        rows += list(mannaminne._email_rows((DEST / copied[mid]["path"]).read_bytes(), set()))
    if not rows:
        print("nothing to add")
        return 0
    with mannaminne.load_conn() as conn, conn.cursor() as cur:
        mannaminne._upsert(cur, rows)
        conn.commit()
        mannaminne._ensure_email_class(conn, cur)
        conn.commit()
    print(f"added {len(rows)} chunk(s) from {len(load_ids(DEST / 'missing.tsv'))} message(s); "
          f"run check again")
    return 0


# --- trash ------------------------------------------------------------------

def backup_holds_copies(dest, count, run=subprocess.run):
    """True when the newest restic snapshot of this Mac holds at least `count`
    .eml files under `dest`. The copy is the only original once Gmail empties
    its trash, so it must sit on a second disk first."""
    script = (f'source "$HOME/.secrets/load-all.sh" >/dev/null 2>&1; '
              f'"$HOME/.local/bin/restic" ls latest --recursive --json "{dest}/eml"')
    p = run(["zsh", "-c", script], capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        return False, p.stderr[-200:]
    n = 0
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "file" and str(d.get("path", "")).endswith(".eml"):
            n += 1
    return n >= count, f"{n} copies in the newest snapshot, {count} needed"


def trash_eligible(dest, plan):
    """The messages that may go to the trash, or raise with the reason none may."""
    try:
        summary = json.loads((dest / "check.json").read_text())
    except FileNotFoundError:
        raise RuntimeError("run check first")
    if (dest / "check.json").stat().st_mtime < (dest / "copied.tsv").stat().st_mtime:
        raise RuntimeError("copies were added after the last check; run check again")
    for key in ("not_copied", "damaged", "missing_from_index"):
        if summary.get(key):
            raise RuntimeError(f"check found {summary[key]} {key.replace('_', ' ')}")
    copied = load_copied(dest)
    spare = set(load_ids(dest / "spare.tsv"))
    done = set(load_ids(dest / "trashed.tsv"))
    return [m for m in plan if m in copied and m not in spare and m not in done]


def batch_modify(ids, add=(), remove=(), run=subprocess.run):
    p = run(["gws", "gmail", "users", "messages", "batchModify", "--params",
             json.dumps({"userId": "me"}), "--json",
             json.dumps({"ids": ids, "addLabelIds": list(add), "removeLabelIds": list(remove)})],
            capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"batchModify failed: {(p.stdout + p.stderr)[-300:]}")


def labels_of(mid, run=subprocess.run):
    p = run(["gws", "gmail", "users", "messages", "get", "--params",
             json.dumps({"userId": "me", "id": mid, "format": "minimal"})],
            capture_output=True, text=True, timeout=60)
    d = json.loads(p.stdout[p.stdout.index("{"):])
    return d.get("labelIds", [])


def trash(ids, dest, run=subprocess.run, log=print, sleep=time.sleep):
    """Label the messages TRASH: one first, read back, then the rest in batches."""
    out = Appender(dest / "trashed.tsv")
    try:
        if not ids:
            log("nothing to trash")
            return 0
        batch_modify(ids[:1], add=["TRASH"], run=run)
        if "TRASH" not in labels_of(ids[0], run=run):
            raise RuntimeError(f"{ids[0]} did not reach the trash; stopping")
        out.add(ids[0])
        rest = ids[1:]
        for i in range(0, len(rest), TRASH_BATCH):
            chunk = rest[i:i + TRASH_BATCH]
            batch_modify(chunk, add=["TRASH"], run=run)
            for m in chunk:
                out.add(m)
            log(f"  {i + len(chunk) + 1}/{len(ids)} in the trash")
            sleep(1)
        return 0
    finally:
        out.close()


NOT_READY = 5


def cmd_trash(args):
    plan = load_ids(DEST / "plan.tsv")
    try:
        ids = trash_eligible(DEST, plan)
    except RuntimeError as e:
        print(f"not trashing: {e}", file=sys.stderr)
        return 6
    ok, why = backup_holds_copies(DEST, len(load_copied(DEST)))
    if not ok:
        print(f"not yet: the backup does not hold every copy ({why})", file=sys.stderr)
        return NOT_READY
    print(f"moving {len(ids)} message(s) to the trash")
    return trash(ids, DEST)


def _ntfy_token():
    for f in sorted((Path.home() / ".secrets").glob("tier-*.env")):
        for raw in f.read_text(errors="replace").splitlines():
            s = raw.strip().removeprefix("export ").strip()
            if s.startswith("NTFY_TOKEN="):
                return s.split("=", 1)[1].strip().strip("\"'")
    return ""


def notify(title, body, run=subprocess.run):
    """Push the outcome to his phone. The token reaches curl on stdin, never on
    a command line or in a log."""
    token = _ntfy_token() if NTFY_URL else ""
    if not token:
        return
    run(["curl", "-fsS", "--max-time", "15", "-K", "-", "-H", f"Title: {title}",
         "--data-binary", body, NTFY_URL],
        input=f'header = "Authorization: Bearer {token}"\n', text=True, capture_output=True)


def cmd_finish(args):
    """Everything after the plan, unattended: copy, check, add what the index
    lacks, wait until a backup snapshot holds every copy, trash, push the result.
    Stops at the first check that fails, and says so on his phone."""
    def stopped(why):
        print(f"finish stopped: {why}", flush=True)
        notify("Gmail-städningen stannade", f"{why}. Inget har flyttats till papperskorgen. "
                                            f"Logg: {DEST}/finish.log")
        return 1
    try:
        code = cmd_copy(args)
        if code:
            return stopped(f"kopieringen avbröts (kod {code})")
        import mannaminne
        plan = load_ids(DEST / "plan.tsv")
        _, cited_index = cited_ids()
        with mannaminne.load_conn() as conn:
            s = check(DEST, plan, conn, cited_index)
        print(json.dumps(s), flush=True)
        if s["not_copied"] or s["damaged"]:
            return stopped(f"{s['not_copied']} okopierade och {s['damaged']} skadade kopior")
        if s["missing_from_index"]:
            # In a fresh process: this run may have loaded the database code half a day
            # ago, and a schema change since then (the 2026-10-17 table split) makes the
            # old write statement fail. A new process reads the code as it is now.
            subprocess.run([sys.executable, os.path.abspath(__file__), "index-missing"], check=True)
            with mannaminne.load_conn() as conn:
                s = check(DEST, plan, conn, cited_index)
            print(json.dumps(s), flush=True)
            if s["missing_from_index"]:
                return stopped(f"{s['missing_from_index']} mejl saknas fortfarande i indexet")
        job = os.environ.get("GMAIL_SWEEP_BACKUP_JOB")
        if job:
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{job}"],
                           capture_output=True)
        for _ in range(args.backup_waits):
            ok, why = backup_holds_copies(DEST, len(load_copied(DEST)))
            print(f"backup: {why}", flush=True)
            if ok:
                break
            time.sleep(1800)
        else:
            return stopped("säkerhetskopian fick aldrig med alla kopior")
        ids = trash_eligible(DEST, plan)
        trash(ids, DEST)
        notify("Gmail-städningen är klar",
               f"{len(ids)} mejl ligger i papperskorgen och kan ångras i 30 dagar. "
               f"Originalen finns på FERMI ({DEST.name}), i säkerhetskopian och i mannaminne.")
        return 0
    except Exception as e:  # the push is the point: an unattended run must not fail silently
        return stopped(f"fel: {type(e).__name__}: {str(e)[:200]}")


def cmd_untrash(args):
    ids = load_ids(DEST / "trashed.tsv")
    for i in range(0, len(ids), TRASH_BATCH):
        batch_modify(ids[i:i + TRASH_BATCH], remove=["TRASH"])
        print(f"  {min(i + TRASH_BATCH, len(ids))}/{len(ids)} back out of the trash")
    return 0


def cmd_status(args):
    plan = load_ids(DEST / "plan.tsv")
    copied = load_copied(DEST)
    pid = (DEST / "copy.pid").read_text().strip() if (DEST / "copy.pid").exists() else None
    print(json.dumps({"planned": len(plan), "copied": len(copied),
                      "gone": len(load_ids(DEST / "gone.tsv")),
                      "failed": len(load_ids(DEST / "failed.tsv")),
                      "trashed": len(load_ids(DEST / "trashed.tsv")),
                      "bytes": sum(r["bytes"] for r in copied.values()),
                      "copy_running": pid}, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    c = sub.add_parser("copy")
    c.add_argument("--workers", type=int, default=WORKERS)
    f = sub.add_parser("finish", help="copy, check, wait for the backup, trash, push the result")
    f.add_argument("--workers", type=int, default=WORKERS)
    f.add_argument("--backup-waits", type=int, default=72, help="half-hour waits for the backup")
    for name in ("check", "index-missing", "trash", "untrash", "status"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    fn = {"plan": cmd_plan, "copy": cmd_copy, "finish": cmd_finish, "check": cmd_check,
          "index-missing": cmd_index_missing, "trash": cmd_trash,
          "untrash": cmd_untrash, "status": cmd_status}[args.cmd]
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
