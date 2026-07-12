#!/usr/bin/env python3
"""mannaminne v2 — full-corpus search over the personal life-corpus, backed by
Postgres + pgvector.

Sources (fully chunked, no truncation): CC session transcripts (noise-filtered),
project/infra docs, Facebook Messenger, AI-chat archives (ChatGPT + Claude),
Simplenote notes. Postgres FTS (tsvector + trigram) is the guaranteed
keyword layer; pgvector (Qwen3-Embedding-4B, Z4-first with Darwin fallback) is
the semantic layer.

Subcommands:
  ingest   discover + chunk + upsert all sources (incremental, hash-based)
  embed    fill NULL embeddings via the Darwin embedder (concurrent)
  search   hybrid keyword + semantic query
  stats    per-source counts + embedding coverage

Aliases (argv0): `ccsearch` scopes to CC sources (session+doc); `minne` /
`mannaminne` search everything. Conn read from ~/.config/mannaminne/db.env.
Design: ~/dotfiles/docs/personal_archives_semantic_search_2026_06_10.md § v2.
"""
from __future__ import annotations
import os, sys, json, glob, hashlib, argparse, concurrent.futures, time, urllib.request, urllib.error, subprocess
import re, email, html as _htmllib
from email import policy as _emailpolicy
import email.utils as _emailutils
from pathlib import Path

HOME = os.path.expanduser("~")

def _env_int(name: str, default: int, min_value: int = 1) -> int:
    try:
        return max(min_value, int(os.environ.get(name, str(default))))
    except ValueError:
        return default

def _env_float(name: str, default: float, min_value: float = 0.1) -> float:
    try:
        return max(min_value, float(os.environ.get(name, str(default))))
    except ValueError:
        return default

EXPLICIT_EMBED_URL = os.environ.get("MANNAMINNE_EMBED_URL")
Z4_EMBED_URL = os.environ.get("MANNAMINNE_Z4_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
DARWIN_EMBED_URL = os.environ.get("MANNAMINNE_DARWIN_EMBED_URL", "http://192.168.4.1:8080/v1/embeddings")
EMBED_URL = EXPLICIT_EMBED_URL or Z4_EMBED_URL
EMBED_MODEL = os.environ.get("MANNAMINNE_EMBED_MODEL", "qwen3-embedding-4b")
EMBED_DIM = _env_int("MANNAMINNE_EMBED_DIM", 1024)  # 8B native=4096; MRL-truncatable to 1024
EMBED_TIMEOUT = _env_float("MANNAMINNE_EMBED_TIMEOUT", 45.0)
EMBED_PROBE_TIMEOUT = _env_float("MANNAMINNE_EMBED_PROBE_TIMEOUT", 5.0)
EMBED_BATCH_SIZE = _env_int("MANNAMINNE_EMBED_BATCH_SIZE", 4)
EMBED_WORKERS = _env_int("MANNAMINNE_EMBED_WORKERS", 2)
EMBED_SELECT_LIMIT = _env_int("MANNAMINNE_EMBED_SELECT_LIMIT", 500)
EMBED_MAX_CHARS = _env_int("MANNAMINNE_EMBED_MAX_CHARS", 750)
# Embedding real session/doc text on the Darwin GTX 1650 runs at ~2 chunks/sec
# (a 4B model, no tensor cores). The Z4 RTX A4000 is ~5-15x faster but is only
# intermittently available. Two knobs keep nightly runs polite + resumable:
#   - EMBED_MAX_SECONDS: stop after a time budget (loop commits per select-batch,
#     so progress persists and the next run resumes). 0 = unlimited (manual runs).
#   - Z4_COOLDOWN_SECS: after the Z4 tunnel fails, skip re-probing it this long.
EMBED_MAX_SECONDS = _env_float("MANNAMINNE_EMBED_MAX_SECONDS", 0.0, min_value=0.0)
Z4_COOLDOWN_SECS = _env_int("MANNAMINNE_Z4_COOLDOWN_SECS", 1800)
Z4_COOLDOWN_FILE = os.path.join(HOME, ".cache/mannaminne/z4-cooldown")
CHUNK_SIZE = _env_int("MANNAMINNE_CHUNK_SIZE", 750)          # chars (~250 tokens for prose)
CHUNK_OVERLAP = _env_int("MANNAMINNE_CHUNK_OVERLAP", 80)     # keeps boundary needles visible
MAX_CHUNKS = _env_int("MANNAMINNE_MAX_CHUNKS", 400)          # per non-doc source object
MAX_DOC_CHUNKS = _env_int("MANNAMINNE_MAX_DOC_CHUNKS", 1200)
SEARCH_KEYWORD_LIMIT = _env_int("MANNAMINNE_SEARCH_KEYWORD_LIMIT", 80)
SEARCH_SEMANTIC_LIMIT = _env_int("MANNAMINNE_SEARCH_SEMANTIC_LIMIT", 80)
SEARCH_EXACT_MAX_TERMS = _env_int("MANNAMINNE_SEARCH_EXACT_MAX_TERMS", 5)
SEARCH_SOFT_TERM_LIMIT = _env_int("MANNAMINNE_SEARCH_SOFT_TERM_LIMIT", 12)
SEARCH_SOFT_PER_TERM_LIMIT = _env_int("MANNAMINNE_SEARCH_SOFT_PER_TERM_LIMIT", 12)
HNSW_EF_SEARCH = _env_int("MANNAMINNE_HNSW_EF_SEARCH", 100)
RRF_K = _env_int("MANNAMINNE_RRF_K", 60)
# Recency signal: on the PROJECT RECORD (docs/sessions/commits/code) a newer hit
# on the same topic usually SUPERSEDES an older one (the brf-auto flip-flop trap:
# a June "reading is the gap" doc sitting two lines from the July "routing
# dominates" refutation). A small additive recency term edges newer above older
# when relevance is near-tied, WITHOUT overriding a strong relevance gap. Applied
# ONLY to the project-record kinds — the life-corpus (2014 emails, old notes)
# must stay findable, so recency is deliberately NOT applied there. Layer-3
# tunable per *Architecture vs parameters* (observe misses, tune without code).
RECENCY_WEIGHT = _env_float("MANNAMINNE_RECENCY_WEIGHT", 0.05, min_value=0.0)
RECENCY_WINDOW_DAYS = _env_float("MANNAMINNE_RECENCY_WINDOW_DAYS", 730.0, min_value=1.0)
_PROJECT_RECORD_KINDS = {"doc", "session", "git_commit", "code"}
QUERY_INSTRUCTION = os.environ.get(
    "MANNAMINNE_QUERY_INSTRUCTION",
    "Given a personal archive search query, retrieve relevant passages, notes, docs, sessions, tasks, or messages that answer it.",
)
_EMBED_URL_CACHE = None

# --- DB ---------------------------------------------------------------------

def load_conn():
    env = {}
    p = Path(HOME) / ".config/mannaminne/db.env"
    for line in p.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    import psycopg
    return psycopg.connect(
        host=env["MANNAMINNE_PG_HOST"], port=env["MANNAMINNE_PG_PORT"],
        dbname=env["MANNAMINNE_PG_DB"], user=env["MANNAMINNE_PG_USER"],
        password=env["MANNAMINNE_PG_PASSWORD"], connect_timeout=10,
    )

# --- helpers ----------------------------------------------------------------

def fix_mojibake(s: str) -> str:
    """Reverse FB's double-encoded UTF-8 (latin1→utf8 reinterpret)."""
    try:
        if all(ord(c) < 256 for c in s):
            return s.encode("latin1").decode("utf8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return s

def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
          max_chunks: int = MAX_CHUNKS):
    text = text.strip()
    if not text:
        return
    n = len(text)
    step = max(1, size - overlap)
    i = idx = 0
    while i < n and idx < max_chunks:
        yield idx, text[i:i + size]
        i += step
        idx += 1

def _single_chunk(text: str):
    """Chunker that yields the whole text as ONE chunk (idx 0). Used for git
    commit messages — per code-retrieval research a commit message is a single
    semantic unit and must NOT be split into char windows. FTS stores the full
    text; the embed step head-truncates the vector (EMBED_MAX_CHARS), which is
    fine because the subject + head of the body carries the signal."""
    text = text.strip()
    if text:
        yield 0, text

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

def chunk_markdown(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
                   max_chunks: int = MAX_DOC_CHUNKS):
    """Heading-aware markdown chunks. Each section chunk carries its heading path
    so semantic search can hit a subsection even when the chunk starts mid-body."""
    text = text.strip()
    if not text:
        return

    headings: list[str] = []
    section_lines: list[str] = []
    emitted = 0

    def emit_section(path: list[str], lines: list[str]):
        body = "\n".join(lines).strip()
        if not body:
            return
        prefix = " > ".join(path)
        prefix_block = f"Heading: {prefix}\n\n" if prefix else ""
        body_size = max(200, size - len(prefix_block))
        for _, ch in chunk(body, size=body_size, overlap=overlap, max_chunks=max_chunks):
            yield f"{prefix_block}{ch}" if prefix_block else ch

    for line in text.splitlines():
        m = _MD_HEADING.match(line)
        if m:
            for ch in emit_section(headings, section_lines):
                if emitted >= max_chunks:
                    return
                yield emitted, ch
                emitted += 1
            level = len(m.group(1))
            title = m.group(2).strip().strip("#").strip()
            headings = headings[:level - 1] + [title]
            section_lines = [line]
        else:
            section_lines.append(line)
    for ch in emit_section(headings, section_lines):
        if emitted >= max_chunks:
            return
        yield emitted, ch
        emitted += 1

def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]

# --- source discovery (yields chunk rows) -----------------------------------
# Each row: (id, source_kind, source_id, chunk_idx, project, title, text, created, content_hash)

def _rows(source_kind, source_id, project, title, full, created, chunker=chunk):
    title = (title or "").replace("\x00", "")          # Postgres text rejects NUL (0x00)
    for idx, ch in chunker(full):
        ch = ch.replace("\x00", "")
        yield (f"{source_id}#{idx}", source_kind, source_id, idx, project,
               title, ch, created, h(ch))

def discover_messenger():
    base = Path(HOME) / "Projects/messenger-archive/your_activity_across_facebook/messages/inbox"
    _skip_if_unchanged("messenger", glob.glob(str(base / "*" / "message_*.json")))
    for d in sorted(glob.glob(str(base / "*"))):
        if not os.path.isdir(d):
            continue
        tid = os.path.basename(d)
        title, parts, newest = "", [], 0
        for f in sorted(glob.glob(os.path.join(d, "message_*.json"))):
            try:
                j = json.load(open(f))
            except Exception:
                continue
            if not title:
                title = fix_mojibake(j.get("title") or "") or ", ".join(
                    fix_mojibake(p.get("name", "")) for p in j.get("participants", []))
            for m in j.get("messages", []):
                newest = max(newest, m.get("timestamp_ms", 0) or 0)
                c = m.get("content")
                if c:
                    parts.append(f"{fix_mojibake(m.get('sender_name',''))}: {fix_mojibake(c)}")
        full = (title + "\n" + "\n".join(parts)).strip()
        if not full:
            continue
        created = time.strftime("%Y-%m-%d", time.gmtime(newest / 1000)) if newest else ""
        yield from _rows("messenger", f"msg:{tid}", "messenger", title or tid[:60], full, created)

def discover_aichat():
    base = Path(HOME) / "Projects/ai-chat-archives"
    _skip_if_unchanged("aichat", glob.glob(str(base / "chatgpt_*/conversations/*.json"))
                       + glob.glob(str(base / "claude_*/conversations/*.json")))
    for f in glob.glob(str(base / "chatgpt_*/conversations/*.json")):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        title = j.get("title") or ""
        msgs = []
        for node in (j.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            ct = (msg.get("create_time") or 0)
            for part in ((msg.get("content") or {}).get("parts") or []):
                if isinstance(part, str) and part.strip():
                    msgs.append((ct, part))
        msgs.sort(key=lambda x: x[0] or 0)
        full = (title + "\n" + "\n".join(p for _, p in msgs)).strip()
        if not full:
            continue
        sid = j.get("conversation_id") or Path(f).stem
        yield from _rows("aichat", f"aichat:cg:{sid}", "chatgpt", title or sid[:60], full, "")
    for f in glob.glob(str(base / "claude_*/conversations/*.json")):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        name = j.get("name") or ""
        created = (j.get("created_at") or "")[:10]
        parts = [f"{m.get('sender','')}: {m.get('text','')}"
                 for m in (j.get("chat_messages") or []) if (m.get("text") or "").strip()]
        full = (name + "\n" + "\n".join(parts)).strip()
        if not full:
            continue
        uuid = j.get("uuid") or Path(f).stem
        yield from _rows("aichat", f"aichat:cl:{uuid}", "claude", name or uuid[:60], full, created)

def discover_notes():
    d = Path(HOME) / "Documents/Simplenote Support Notes"
    _skip_if_unchanged("note", glob.glob(str(d / "*.txt")))
    for f in glob.glob(str(d / "*.txt")):
        try:
            content = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if not content.strip():
            continue
        name = Path(f).stem
        yield from _rows("note", f"note:{name}", "simplenote", name, content, "")

def discover_docs():
    scans = [(Path(HOME) / "Projects", "*/docs/**/*.md"), (Path(HOME) / "dotfiles", "docs/**/*.md")]
    _skip_if_unchanged("doc", [f for base, pat in scans
                               for f in glob.glob(str(base / pat), recursive=True)])
    for base, pat in scans:
        for f in glob.glob(str(base / pat), recursive=True):
            if any(x in f for x in ("/archive/", "/vendor/", "/node_modules/", "/.")):
                continue
            try:
                if os.path.getsize(f) > 600_000:
                    continue
                content = open(f, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if not content.strip():
                continue
            project = "dotfiles" if str(base).endswith("dotfiles") else Path(f).relative_to(base).parts[0]
            rel = os.path.relpath(f, base)
            yield from _rows("doc", f"doc:{project}:{rel}", project, Path(f).stem, content, "",
                             chunker=chunk_markdown)

    # Global agent instructions are high-value retrieval context but live outside
    # the normal docs roots. Index the canonical file only; ~/.codex/AGENTS.md is
    # a symlink to the same shared guidance on this machine.
    global_claude = Path(HOME) / ".claude/CLAUDE.md"
    if global_claude.exists():
        try:
            content = global_claude.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        if content.strip():
            yield from _rows("doc", "doc:global-claude:CLAUDE.md", "global-claude",
                             "CLAUDE.md", content, "", chunker=chunk_markdown)

_NOISE = ("<system-reminder>", "This session is being continued", "Caveat:",
          "# CLAUDE.md", "Codebase and user instructions are shown below",
          "<command-name>", "<local-command-stdout>", "DO NOT respond to these")

class SourceUnavailable(Exception):
    """A source's storage location is temporarily inaccessible (e.g. an external
    volume is unmounted). Raised so cmd_ingest SKIPS the kind without running the
    orphan-prune that would otherwise wipe its chunks from the index."""


_FINGERPRINT_FILE = os.path.join(HOME, ".cache/mannaminne/source_fingerprints.json")
_PENDING_FINGERPRINTS = {}


class SourceUnchanged(Exception):
    """Raised at the top of a discoverer when its on-disk inputs are byte-for-byte
    unchanged since the last successful ingest (same set of path + mtime + size).
    cmd_ingest catches it, SKIPS the source entirely (no re-read / re-chunk /
    re-upsert) and excludes it from the orphan-prune so its chunks are kept.
    Fingerprint errs toward re-ingesting: a superset of inputs is used, so we
    only skip when NOTHING changed — never wrongly skip a changed source."""


def _fingerprint_paths(paths):
    entries = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((str(p), st.st_mtime_ns, st.st_size))
    entries.sort()
    return h(repr(entries))


def _load_fingerprints():
    try:
        with open(_FINGERPRINT_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_fingerprint(kind, fp):
    try:
        os.makedirs(os.path.dirname(_FINGERPRINT_FILE), exist_ok=True)
        store = _load_fingerprints()
        store[kind] = fp
        tmp = f"{_FINGERPRINT_FILE}.tmp"
        with open(tmp, "w") as fh:
            json.dump(store, fh)
        os.replace(tmp, _FINGERPRINT_FILE)
    except Exception:
        pass


def _skip_if_unchanged(kind, paths):
    """Top-of-discoverer guard: raise SourceUnchanged if the source's input files
    are unchanged since the last successful ingest. Otherwise stash the new
    fingerprint as PENDING — cmd_ingest persists it only AFTER the kind ingests
    to completion (so a mid-run failure re-ingests next time, never falsely skips)."""
    paths = list(paths)
    fp = _fingerprint_paths(paths)
    if paths and _load_fingerprints().get(kind) == fp:
        raise SourceUnchanged(kind)
    _PENDING_FINGERPRINTS[kind] = fp


def _volume_available(path: str) -> bool:
    """For /Volumes/<vol>/... paths the volume must be mounted; all other paths
    are always 'available'."""
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return os.path.ismount(os.path.join("/Volumes", parts[2]))
    return True


def _session_paths():
    """CC transcript roots, live first. Live = ~/.claude/projects (recent);
    FERMI archive = older sessions moved off the small internal SSD by
    nightly-sweep. Both are scanned so archived sessions stay indexed; source_id
    is keyed on the session UUID, so a session present in both dedups at upsert.
    Override with MANNAMINNE_SESSION_PATHS (os.pathsep-separated, live first)."""
    default = os.pathsep.join([
        os.path.join(HOME, ".claude/projects"),
        "/Volumes/FERMI/MacMini-archives additions/claude-sessions-archive",
    ])
    return [p for p in os.environ.get("MANNAMINNE_SESSION_PATHS", default).split(os.pathsep)
            if p.strip()]


def discover_sessions():
    """CC transcripts, noise-filtered: keep human + assistant natural-language
    text; drop tool calls, injected CLAUDE.md, system reminders, huge boilerplate.
    Scans live + FERMI-archive roots (see _session_paths). If a configured
    external root's volume is unmounted, raises SourceUnavailable so ingest skips
    sessions WITHOUT pruning the index (the volume is temporarily gone, not the
    data)."""
    paths = _session_paths()
    for base in paths:
        if not _volume_available(base):
            raise SourceUnavailable(f"session source volume unmounted: {base}")
    _skip_if_unchanged("session", [f for base in paths
                                   for f in glob.glob(os.path.join(base, "*/*.jsonl"))])
    seen_sids = set()
    for base in paths:
        for f in sorted(glob.glob(os.path.join(base, "*/*.jsonl"))):
            if "subagent" in f:
                continue
            sid = Path(f).stem
            if sid in seen_sids:   # same session in live + archive → index once (live wins)
                continue
            seen_sids.add(sid)
            proj = Path(f).parent.name.rsplit("-", 1)[-1]
            parts, created = [], ""
            try:
                fh = open(f, encoding="utf-8", errors="replace")
            except Exception:
                continue
            with fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("isCompactSummary"):
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    if not created:
                        created = (o.get("timestamp") or "")[:10]
                    msg = o.get("message") or {}
                    cont = msg.get("content")
                    texts = []
                    if isinstance(cont, str):
                        texts = [cont]
                    elif isinstance(cont, list):
                        texts = [b.get("text", "") for b in cont
                                 if isinstance(b, dict) and b.get("type") == "text"]
                    for t in texts:
                        if not t or len(t) > 12000:        # skip giant boilerplate dumps
                            continue
                        if any(mark in t for mark in _NOISE):
                            continue
                        parts.append(f"{typ}: {t}")
            full = "\n".join(parts).strip()
            if not full:
                continue
            title = (parts[0][:80] if parts else sid)
            yield from _rows("session", f"session:{sid}", proj, title, full, created)

# --- email (mbox: Gmail Takeout + curated subsets) --------------------------
# Streaming parser — never loads the whole file (the Gmail Takeout is 4.7 GB).
# Dedups by Message-ID across all mboxes; reports unique-new per file.

MBOX_SOURCES = [
    ("takeout2014",
     "/Volumes/FERMI/MacMini-archives additions/demeter_2017_drive/"
     "emails_documents_2014/All mail Including Spam and Trash-2.mbox"),
    ("deliberus",
     os.path.join(HOME, "Projects/deliberus/archive/demeter_2017_dropbox/"
                        "excavated_emails/deliberus_relevant.mbox")),
]

def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _htmllib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()

_ENVELOPE = re.compile(rb"^From \S+ (Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z][a-z] +\d")

def _iter_mbox(path):
    """Yield raw message bytes one at a time. Splits on a `From <id> <Weekday>
    <Mon> <DD> …` envelope line (Gmail Takeout / Apple Mail format) — these are
    NOT reliably blank-preceded, so match the envelope shape directly. The strict
    regex avoids false splits on body lines that merely start with 'From '.
    Memory-safe for multi-GB mboxes (streams one message at a time)."""
    buf = bytearray()
    with open(path, "rb") as fh:
        for line in fh:
            if _ENVELOPE.match(line):
                if buf:
                    yield bytes(buf)
                    buf = bytearray()
            buf += line
    if buf:
        yield bytes(buf)

def _email_body(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            return _strip_html(content) if part.get_content_subtype() == "html" else content
    except Exception:
        pass
    out = []                                   # fallback: walk parts
    try:
        for p in msg.walk():
            ct = p.get_content_type()
            try:
                if ct == "text/plain":
                    out.append(p.get_content())
                elif ct == "text/html":
                    out.append(_strip_html(p.get_content()))
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(out)

def discover_email():
    _skip_if_unchanged("email", [path for _label, path in MBOX_SOURCES])
    seen = set()
    for label, path in MBOX_SOURCES:
        if not os.path.exists(path):
            print(f"  (email/{label}: missing at {path})", flush=True)
            continue
        nmsg = nuniq = nskip = 0
        for raw in _iter_mbox(path):
            nmsg += 1
            if raw.startswith(b"From "):           # strip mbox envelope separator
                nl = raw.find(b"\n")
                raw = raw[nl + 1:] if nl != -1 else raw
            try:
                msg = email.message_from_bytes(raw, policy=_emailpolicy.default)
            except Exception:
                nskip += 1; continue
            mid = (str(msg.get("Message-ID") or msg.get("Message-Id") or "")).strip().strip("<>")
            subj = str(msg.get("Subject") or "").strip()
            frm = str(msg.get("From") or "").strip()
            to = str(msg.get("To") or "").strip()
            datehdr = str(msg.get("Date") or "").strip()
            body = _email_body(msg) or ""
            if not subj and not body:
                nskip += 1; continue
            key = mid or h(f"{datehdr}|{frm}|{subj}|{len(body)}")
            if key in seen:
                continue
            seen.add(key); nuniq += 1
            created = ""
            try:
                dt = _emailutils.parsedate_to_datetime(datehdr)
                if dt:
                    created = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
            title = subj or (frm[:60] if frm else "(no subject)")
            full = f"{subj}\nFrom: {frm}\nTo: {to}\nDate: {datehdr}\n\n{body}"
            yield from _rows("email", f"email:{h(key)}", "gmail", title, full, created)
        print(f"  email/{label}: {nmsg} msgs → {nuniq} unique-new, {nskip} skipped", flush=True)

# --- Things 3 (the 7k-task goldmine — local SQLite, read-only) ---------------
# Things3 stores everything in TMTask (type 0=task, 1=project, 2=heading) under a
# per-install Group Container. We index non-trashed TASKS (open + completed — the
# completed ones are historical needles), with area/project title as context.
# creationDate is a Unix epoch (verified: 2013–2025 range), not Core Data.

def discover_things3():
    import sqlite3
    base = Path(HOME) / "Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac"
    dbs = sorted(glob.glob(str(base / "ThingsData-*/Things Database.thingsdatabase/main.sqlite")))
    if not dbs:
        print("  (things3: no DB found)", flush=True)
        return
    _skip_if_unchanged("things3", dbs)
    db = dbs[-1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT t.uuid AS uuid, t.title AS title, t.notes AS notes, t.status AS status,
               t.creationDate AS created, a.title AS area_title, p.title AS project_title
        FROM TMTask t
        LEFT JOIN TMArea a ON t.area = a.uuid
        LEFT JOIN TMTask p ON t.project = p.uuid
        WHERE t.trashed = 0 AND t.type = 0
    """)
    n = 0
    for r in cur.fetchall():
        title = (r["title"] or "").strip()
        notes = (r["notes"] or "").strip()
        if not title and not notes:
            continue
        ctx = (r["project_title"] or r["area_title"] or "").strip()
        status = "done" if r["status"] == 3 else "open"
        created = ""
        if r["created"]:
            try:
                created = time.strftime("%Y-%m-%d", time.gmtime(float(r["created"])))
            except Exception:
                pass
        head = f"[{ctx}] {title} ({status})" if ctx else f"{title} ({status})"
        full = f"{head}\n{notes}" if notes else head
        yield from _rows("things3", f"things3:{r['uuid']}", "things3", title or ctx or "task", full, created)
        n += 1
    con.close()
    print(f"  things3: {n} tasks", flush=True)

# --- Fyr (the aggregator brain — FalkorDB task graph on Darwin, read-only) ----
# Fyr aggregates personal + project todos as :Task nodes in a per-user FalkorDB
# graph (fyr-<uuid>) on darwin.home:6380. One read captures both the TickTick
# life-todos Fyr already mirrors AND the project TODO.md tasks. We index
# name + summary (+ source/status as context). Tasks carry no embeddings in Fyr
# (structural only); semantic comes from mannaminne's own embed once it resumes.

def discover_fyr():
    try:
        from falkordb import FalkorDB
    except ImportError:
        print("  (fyr: falkordb client not installed — pip install falkordb)", flush=True)
        return
    host = os.environ.get("MANNAMINNE_FALKOR_HOST", "darwin.home")
    try:
        client = FalkorDB(host=host, port=6380, password="falkordb")
        graphs = [g for g in client.list_graphs() if str(g).startswith("fyr-")]
    except Exception as e:
        print(f"  (fyr: FalkorDB unreachable at {host}:6380: {type(e).__name__})", flush=True)
        return
    if not graphs:
        print("  (fyr: no fyr-* graph found)", flush=True)
        return
    n = 0
    for gname in graphs:
        g = client.select_graph(gname)
        try:
            res = g.query("MATCH (t:Task) RETURN t.uuid, t.name, t.summary, "
                          "t.status, t.external_source, t.created_at")
        except Exception:
            continue
        for rec in res.result_set:
            uuid, name, summary, status, ext, created = (list(rec) + [None] * 6)[:6]
            name = (name or "").strip()
            summary = (summary or "").strip()
            if not name and not summary:
                continue
            head = f"{name} [{ext or 'fyr'}/{status or '?'}]"
            full = f"{head}\n{summary}" if summary else head
            created_s = ""
            if created:
                try:
                    v = float(created)
                    if v > 1e11:                 # Fyr stores created_at in epoch MS
                        v /= 1000.0
                    created_s = time.strftime("%Y-%m-%d", time.gmtime(v))
                except Exception:
                    created_s = str(created)[:10]
            yield from _rows("fyr", f"fyr:{uuid}", "fyr", name or "task", full, created_s)
            n += 1
    print(f"  fyr: {n} tasks", flush=True)

# --- Screenshots + Photos (Apple Vision OCR via ocrmac, + osxphotos labels) ---
# Two image troves, both indexed as source_kind 'screenshot':
#  (1) Mac screenshots archived on FERMI (~5k PNGs)
#  (2) iPhone screenshots + label-rich photos in the Photos library (originals
#      local on FERMI). osxphotos gives file paths + Apple's scene/object labels;
#      ocrmac (Apple Vision) extracts the text (Apple's own OCR isn't exposed for
#      this non-active library). OCR is cached to disk so re-runs skip the work.
#  OCR only screenshots (text-bearing); index labels for ALL photos.

_OCR_CACHE = Path(HOME) / ".cache/mannaminne/ocr_cache.json"
_FERMI_SS = "/Volumes/FERMI/MacMini-archives additions/Screenshots"
_FERMI_PHOTOLIB = "/Volumes/FERMI/Photos Library.photoslibrary"

def _ocr_text(path, cache):
    if not path:
        return ""
    if path in cache:
        return cache[path]
    txt = ""
    try:
        from ocrmac import ocrmac
        res = ocrmac.OCR(path, framework="vision").recognize()
        txt = " ".join(t for t, _c, _b in res).strip()
    except Exception:
        txt = ""
    cache[path] = txt
    return txt

def discover_screenshots():
    import json as _json
    cache = {}
    if _OCR_CACHE.exists():
        try:
            cache = _json.loads(_OCR_CACHE.read_text())
        except Exception:
            cache = {}
    _OCR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    limit = int(os.environ.get("MANNAMINNE_SS_LIMIT", "0"))   # >0 = smoke-test cap
    n = 0

    def _flush():
        try:
            _OCR_CACHE.write_text(_json.dumps(cache))
        except Exception:
            pass

    # (1) Mac screenshots on FERMI
    for f in sorted(glob.glob(os.path.join(_FERMI_SS, "*.png")) +
                    glob.glob(os.path.join(_FERMI_SS, "*.jpg"))):
        if limit and n >= limit:
            break
        n += 1
        txt = _ocr_text(f, cache)
        if n % 200 == 0:
            _flush()
        if not txt:
            continue
        name = os.path.basename(f)
        created = time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(f)))
        yield from _rows("screenshot", f"ss:mac:{name}", "mac-screenshot", name,
                         f"{name}\n{txt}", created)

    # (2) iPhone / Photos library — OCR screenshots, label everything
    if not (limit and n >= limit):
        try:
            import osxphotos
            db = osxphotos.PhotosDB(_FERMI_PHOTOLIB)
        except Exception as e:
            print(f"  (photos: osxphotos unavailable: {type(e).__name__})", flush=True)
            db = None
        if db:
            for p in db.photos():
                if limit and n >= limit:
                    break
                n += 1
                labels = ", ".join((p.labels or [])[:10])
                txt = _ocr_text(p.path, cache) if (p.path and p.screenshot) else ""
                if not txt and not labels:
                    continue
                lbl = "iphone-screenshot" if p.screenshot else "photo"
                head = p.original_filename or (p.uuid[:10] if p.uuid else "photo")
                created = p.date.strftime("%Y-%m-%d") if p.date else ""
                body = f"{head} [{labels}]" + (f"\n{txt}" if txt else "")
                yield from _rows("screenshot", f"photo:{p.uuid}", lbl, head, body, created)
                if n % 200 == 0:
                    _flush()
    _flush()
    print(f"  screenshots/photos: {n} images processed (OCR cached at {_OCR_CACHE})", flush=True)

# --- git commit history (the 2,924-commit-per-repo record — read-only) --------
# The "was this tried / decided / refuted before?" prior-art layer. Commit
# subject+body holds change-rationale that docs often miss; it's the append-only
# NL-over-prose corpus where semantic search unambiguously beats agentic grep
# (2026 field research). One chunk per commit (never split — research §1). LOCAL
# repos only (skip symlinks + /Volumes) so an unmounted FERMI archive can never
# trigger the orphan-prune of previously-indexed commits.

_GIT_COMMIT_MAX_CHARS = _env_int("MANNAMINNE_GIT_COMMIT_MAX_CHARS", 4000)

def _git_repos():
    # Allowlist override: MANNAMINNE_GIT_REPOS=brf-auto,dotfiles restricts to
    # those repo names (comma/os.pathsep separated). Unset → all local repos.
    # Curation: vendored/forked third-party clones carry upstream commit history
    # that is noise, not Fredrik's decision record — the allowlist is how a
    # future run scopes to his OWN repos once the set is chosen.
    allow = os.environ.get("MANNAMINNE_GIT_REPOS", "").replace(os.pathsep, ",")
    allow_set = {n.strip() for n in allow.split(",") if n.strip()} or None
    if allow_set is None:
        # Persistent curated allowlist (used by the nightly refresh + default
        # manual runs). Env override above wins for one-off scoped runs.
        try:
            cfg = (Path(HOME) / ".config/mannaminne/code_repos.txt").read_text()
            names = {ln.strip() for ln in cfg.splitlines()
                     if ln.strip() and not ln.startswith("#")}
            allow_set = names or None
        except Exception:
            allow_set = None
    roots = []
    for d in sorted(glob.glob(str(Path(HOME) / "Projects/*"))):
        p = Path(d)
        try:
            # Symlinks are FOLLOWED, not skipped — a project archived to a FERMI
            # symlink (project-archival cold-pass) stays indexed while the volume
            # is mounted ((p/'.git').is_dir() resolves through the symlink). A
            # broken symlink (volume unmounted) fails is_dir() → skipped, and the
            # project-scoped prune in cmd_ingest PRESERVES its chunks. Together
            # these make the index survive archive→restore cycles.
            if not (p / ".git").is_dir():
                continue
        except OSError:
            continue
        if allow_set is None or p.name in allow_set:
            roots.append(p)
    dotfiles = Path(HOME) / "dotfiles"
    if (dotfiles / ".git").is_dir() and (allow_set is None or "dotfiles" in allow_set):
        roots.append(dotfiles)
    return roots

def discover_git_commits():
    repos = _git_repos()
    # .git/logs/HEAD grows by one line per ref update, so its (mtime,size)
    # fingerprint changes exactly when new commits land → clean incremental skip.
    _skip_if_unchanged("git_commit", [str(r / ".git/logs/HEAD") for r in repos])
    n = 0
    for repo in repos:
        name = repo.name
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--no-merges", "-z",
                 "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b"],
                capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace").stdout
        except Exception as e:
            print(f"  (git_commit/{name}: git log failed: {type(e).__name__})", flush=True)
            continue
        for rec in out.split("\x00"):
            if not rec.strip():
                continue
            parts = rec.split("\x1f")
            if len(parts) < 5:
                continue
            sha, author, date, subject, body = (parts + [""] * 5)[:5]
            sha = sha.strip()
            if not sha:
                continue
            created = (date or "")[:10]
            header = f"commit {sha[:8]} · {name} · {created} · {author}".strip()
            full = f"{header}\n{subject}\n\n{body}".strip()[:_GIT_COMMIT_MAX_CHARS]
            yield from _rows("git_commit", f"gitcommit:{name}:{sha}", name,
                             (subject or sha[:8])[:80], full, created,
                             chunker=_single_chunk)
            n += 1
    print(f"  git_commit: {n} commits from {len(repos)} repos", flush=True)

# --- code (cAST tree-sitter symbol chunks — the missing "where is X / what does
# X do" layer) ---------------------------------------------------------------
# Reuses discover plumbing but chunks via code_chunker (cAST split-then-merge +
# metadata header). v1 lands as a `code` source_kind in the existing `chunks`
# table (not a dedicated code_chunks table) — fastest path, all plumbing reused,
# and the project + source_kind filters cleanly separate code from life-corpus
# at query time. Dedicated symbol/line columns are the documented later refinement.
# Budget defaults to ~700 nw-chars to FIT the Darwin ctx-512 embedder (the
# standing path while the Z4 ctx-8192 accelerator is VPN-pending).

_CODE_MAX_FILE_BYTES = _env_int("MANNAMINNE_CODE_MAX_FILE_BYTES", 200_000)
_CODE_SKIP_SUBSTR = ("/vendor/", "/node_modules/", "/.git/", "/dist/", "/build/",
                     "/tmp/", "/coverage/", "/.venv/", "/target/", ".min.js", ".min.css")

def discover_code():
    import code_chunker
    max_chars = _env_int("MANNAMINNE_CODE_MAX_CHARS", 700)
    repos = _git_repos()
    files = []
    for repo in repos:
        try:
            tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                                     capture_output=True, text=True, timeout=60,
                                     encoding="utf-8", errors="replace").stdout.splitlines()
        except Exception:
            continue
        for rel in tracked:
            if code_chunker.guess_language(rel) is None:
                continue
            if any(s in "/" + rel for s in _CODE_SKIP_SUBSTR):
                continue
            files.append((repo, rel))
    _skip_if_unchanged("code", [str(repo / rel) for repo, rel in files])
    n = 0
    for repo, rel in files:
        fp = repo / rel
        try:
            if fp.stat().st_size > _CODE_MAX_FILE_BYTES:
                continue
            src = fp.read_bytes()
            created = time.strftime("%Y-%m-%d", time.gmtime(fp.stat().st_mtime))
        except Exception:
            continue
        name = repo.name
        try:
            chunks = code_chunker.chunk_code(src, rel, max_chars=max_chars)
        except Exception:
            continue
        sid = f"code:{name}:{rel}"
        for ci, ch in enumerate(chunks):
            text = ch.text.replace("\x00", "")
            title = (f"{rel} › {ch.symbol}" if ch.symbol else rel)[:120].replace("\x00", "")
            yield (f"{sid}#{ci}", "code", sid, ci, name, title, text, created, h(text))
            n += 1
    print(f"  code: {n} chunks from {len(repos)} repos", flush=True)

ALL = {"messenger": discover_messenger, "aichat": discover_aichat,
       "note": discover_notes, "doc": discover_docs, "session": discover_sessions,
       "email": discover_email, "things3": discover_things3, "fyr": discover_fyr,
       "screenshot": discover_screenshots, "git_commit": discover_git_commits,
       "code": discover_code}

# --- ingest -----------------------------------------------------------------

def cmd_ingest(args):
    conn = load_conn()
    cur = conn.cursor()
    kinds = args.sources or list(ALL)
    seen, total, completed_kinds = [], 0, []
    for kind in kinds:
        n, batch = 0, []
        try:
            for row in ALL[kind]():
                seen.append(row[0]); batch.append(row)
                if len(batch) >= 500:
                    _upsert(cur, batch); conn.commit(); n += len(batch); batch = []
            if batch:
                _upsert(cur, batch); conn.commit(); n += len(batch)
        except SourceUnavailable as e:
            conn.rollback()
            print(f"  {kind}: SKIPPED — {e} (existing chunks preserved, NOT pruned)", flush=True)
            continue
        except SourceUnchanged:
            print(f"  {kind}: unchanged on disk — skipped (no re-ingest)", flush=True)
            continue
        completed_kinds.append(kind)
        if kind in _PENDING_FINGERPRINTS:          # persist only after full success
            _save_fingerprint(kind, _PENDING_FINGERPRINTS[kind])
        total += n
        print(f"  {kind}: {n} chunks upserted", flush=True)
    # orphan cleanup: drop chunks of the COMPLETED kinds NOT produced this run
    # (source object deleted, or shrank below a chunk_idx). Temp-table anti-join.
    # Kinds skipped via SourceUnavailable are excluded so an unmounted external
    # volume never prunes its own index.
    if seen and completed_kinds:
        cur.execute("CREATE TEMP TABLE _seen (id text)")
        with cur.copy("COPY _seen (id) FROM STDIN") as cp:
            for x in seen:
                cp.write_row((x,))
        cur.execute("CREATE INDEX ON _seen (id)")
        pruned = 0
        # Non-code kinds: kind-scoped prune (each has its own SourceUnavailable/
        # SourceUnchanged guard; a skipped kind never reaches completed_kinds).
        other = [k for k in completed_kinds if k not in ("code", "git_commit")]
        if other:
            cur.execute("DELETE FROM chunks WHERE source_kind = ANY(%s) "
                        "AND NOT EXISTS (SELECT 1 FROM _seen s WHERE s.id = chunks.id)", (other,))
            pruned += cur.rowcount
        # code/git_commit: PROJECT-scoped prune — only within repos actually
        # processed THIS run. A repo absent this run (archived to a FERMI symlink
        # whose volume is unmounted, temporarily removed, or deleted) is NOT in
        # processed_projects, so its chunks are PRESERVED — the index survives
        # archive→restore cycles. Within a processed repo a deleted file still
        # prunes (project matches, id unseen). Subsumes the old env-allowlist
        # skip: an env-scoped subset run only sees its own projects, so it can
        # only prune its own repos.
        code_kinds = [k for k in completed_kinds if k in ("code", "git_commit")]
        processed_projects = sorted({
            x.split(":", 2)[1] for x in seen
            if (x.startswith("code:") or x.startswith("gitcommit:")) and x.count(":") >= 2
        })
        if code_kinds and processed_projects:
            cur.execute("DELETE FROM chunks WHERE source_kind = ANY(%s) "
                        "AND project = ANY(%s) "
                        "AND NOT EXISTS (SELECT 1 FROM _seen s WHERE s.id = chunks.id)",
                        (code_kinds, processed_projects))
            pruned += cur.rowcount
        cur.execute("DROP TABLE _seen"); conn.commit()
        print(f"  pruned {pruned} orphaned chunks", flush=True)
    print(f"ingest done: {total} chunks. (full-text search is live now.)", flush=True)

def _upsert(cur, rows):
    # Upsert; if the chunk text changed (hash differs) reset its embedding so it re-embeds.
    cur.executemany(
        """INSERT INTO chunks (id,source_kind,source_id,chunk_idx,project,title,text,created,content_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET
             text=EXCLUDED.text, title=EXCLUDED.title, project=EXCLUDED.project,
             created=EXCLUDED.created,
             embedding = CASE WHEN chunks.content_hash <> EXCLUDED.content_hash
                              THEN NULL ELSE chunks.embedding END,
             content_hash=EXCLUDED.content_hash""",
        rows)

# --- embed ------------------------------------------------------------------

def _embed_urls():
    if EXPLICIT_EMBED_URL:
        return [EXPLICIT_EMBED_URL]
    urls = []
    for u in (Z4_EMBED_URL, DARWIN_EMBED_URL):
        if u and u not in urls:
            urls.append(u)
    return urls

_CTX_EXCEEDED_RE = re.compile(
    r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)")

# Diagnostics: why the most recently-dropped chunk failed permanently, so the
# cmd_embed "no progress" message stops blind-guessing "endpoint down/ceded".
_LAST_DROP_REASON = None


class EmbedContextExceeded(Exception):
    """A single input is denser than the ACTIVE endpoint's token context (e.g.
    750 chars of OCR/code text = >512 tokens on the Darwin ctx-512 server, while
    Z4 at ctx-8192 takes it whole). Carries the parsed budget so the caller can
    truncate-and-retry instead of dropping the chunk to NULL forever."""
    def __init__(self, budget_tokens, request_tokens=0, detail=""):
        self.budget_tokens = int(budget_tokens)
        self.request_tokens = int(request_tokens)
        self.detail = detail
        super().__init__(f"input {self.request_tokens} tokens exceeds endpoint "
                         f"context {self.budget_tokens}")


def _record_drop(cid, exc):
    global _LAST_DROP_REASON
    _LAST_DROP_REASON = f"{type(exc).__name__}: {str(exc)[:140]}"


def _ctx_exceeded_from(http_error):
    """Return an EmbedContextExceeded if this HTTPError is llama.cpp's
    'request (N tokens) exceeds the available context size (M tokens)' 400,
    else None (so the caller re-raises the original error unchanged)."""
    if getattr(http_error, "code", None) != 400:
        return None
    try:
        body = http_error.read().decode("utf-8", "replace")
    except Exception:
        return None
    mm = _CTX_EXCEEDED_RE.search(body)
    if not mm:
        return None
    return EmbedContextExceeded(budget_tokens=mm.group(2),
                               request_tokens=mm.group(1), detail=body[:200])


def _post_embed(url, texts, timeout):
    body = json.dumps({"input": texts, "model": EMBED_MODEL}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        ctx = _ctx_exceeded_from(e)
        if ctx is not None:
            raise ctx from e
        raise
    return [d["embedding"][:EMBED_DIM] for d in data["data"]]

def _z4_in_cooldown():
    """True if the Z4 tunnel failed recently and is still inside its cooldown."""
    try:
        with open(Z4_COOLDOWN_FILE) as fh:
            return time.time() < float(fh.read().strip())
    except Exception:
        return False

def _z4_set_cooldown():
    try:
        os.makedirs(os.path.dirname(Z4_COOLDOWN_FILE), exist_ok=True)
        with open(Z4_COOLDOWN_FILE, "w") as fh:
            fh.write(str(time.time() + Z4_COOLDOWN_SECS))
    except Exception:
        pass

def _z4_clear_cooldown():
    try:
        os.remove(Z4_COOLDOWN_FILE)
    except Exception:
        pass

def _embed_batch(texts):
    global _EMBED_URL_CACHE
    errors = []
    ctx_exc = None
    if EXPLICIT_EMBED_URL:
        candidates = [EXPLICIT_EMBED_URL]
    else:
        # Prefer the Z4 tunnel whenever available (much faster). After a Z4
        # failure a file-based cooldown skips re-probing it for Z4_COOLDOWN_SECS,
        # so a long Darwin run does not pay the probe timeout on every batch; the
        # cooldown auto-expires so the run migrates back to Z4 once it returns.
        candidates = _embed_urls()
        if _z4_in_cooldown():
            candidates = [u for u in candidates if u != Z4_EMBED_URL] or candidates
    for url in candidates:
        try:
            if EXPLICIT_EMBED_URL or url == _EMBED_URL_CACHE or url == DARWIN_EMBED_URL:
                timeout = EMBED_TIMEOUT
            else:
                timeout = EMBED_PROBE_TIMEOUT
            out = _post_embed(url, texts, timeout)
            _EMBED_URL_CACHE = url
            if url == Z4_EMBED_URL:
                _z4_clear_cooldown()
            return out
        except EmbedContextExceeded as e:
            # The endpoint is HEALTHY; the input is too big for its context. Do
            # NOT cooldown Z4 for this. Remember it so the caller can truncate +
            # retry, and try any remaining (possibly larger-ctx) endpoint.
            ctx_exc = e
            errors.append(f"{url}: context-exceeded "
                          f"({e.request_tokens}>{e.budget_tokens} tok)")
            if EXPLICIT_EMBED_URL:
                break
            if url == _EMBED_URL_CACHE:
                _EMBED_URL_CACHE = None
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {e}")
            if url == Z4_EMBED_URL:
                _z4_set_cooldown()
            if EXPLICIT_EMBED_URL:
                break
            if url == _EMBED_URL_CACHE:
                _EMBED_URL_CACHE = None
    if ctx_exc is not None:
        raise ctx_exc
    raise RuntimeError("all embedding endpoints failed: " + " | ".join(errors))

def _embed_query_text(q: str) -> str:
    # Qwen3 embeddings are instruction-aware on the query side. Documents stay
    # raw; only the query receives the retrieval task framing.
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {q}"

def _embed_index_text(text: str) -> str:
    return text[:EMBED_MAX_CHARS]

def _embed_pairs(pair):
    if not pair:
        return []
    try:
        embs = _embed_batch([_embed_index_text(t) for _, t in pair])
        return [(pair[i][0], embs[i]) for i in range(len(pair))]
    except EmbedContextExceeded as e:
        # One item in the batch is over the endpoint's token context. Bisect to
        # isolate it, then truncate-and-retry that single chunk.
        if len(pair) == 1:
            return _embed_one_truncated(pair[0], e)
        mid = max(1, len(pair) // 2)
        return _embed_pairs(pair[:mid]) + _embed_pairs(pair[mid:])
    except Exception as e:
        if len(pair) == 1:
            _record_drop(pair[0][0], e)
            return []
        mid = max(1, len(pair) // 2)
        return _embed_pairs(pair[:mid]) + _embed_pairs(pair[mid:])


def _embed_one_truncated(item, exc):
    """Shrink a single over-context chunk to fit the endpoint's parsed token
    budget and retry, so it gets a (head) semantic vector instead of staying
    NULL (FTS-only) forever. Endpoint-aware by construction: we only arrive here
    because an endpoint actually rejected the full text — Z4 (ctx-8192) takes
    these whole, so truncation is inherently the Darwin-only (ctx-512) path.
    A later Z4-fidelity re-embed pass could recompute these at full length."""
    cid, text = item
    cur = _embed_index_text(text)
    budget = exc.budget_tokens or 512
    req = exc.request_tokens or (len(cur) // 2) or 1
    for _ in range(6):
        target = max(1, int(len(cur) * (budget * 0.88) / max(req, 1)))
        if target >= len(cur):
            target = max(1, int(len(cur) * 0.85))   # guarantee forward progress
        cur = cur[:target]
        try:
            embs = _embed_batch([cur])
            return [(cid, embs[0])]
        except EmbedContextExceeded as e2:
            budget = e2.budget_tokens or budget
            req = e2.request_tokens or req
        except Exception as e:
            _record_drop(cid, e)
            return []
    _record_drop(cid, RuntimeError(
        f"still over-context after truncation (budget {budget} tok)"))
    return []

def _vec(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

def cmd_embed(args):
    conn = load_conn()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL")
    pending = cur.fetchone()[0]
    max_total = getattr(args, "limit", 0) if args else 0
    print(f"embedding: {pending} chunks pending", flush=True)
    done = 0
    start = time.monotonic()
    while True:
        select_limit = EMBED_SELECT_LIMIT
        if max_total:
            remaining = max_total - done
            if remaining <= 0:
                break
            select_limit = min(select_limit, remaining)
        cur.execute("SELECT id,text FROM chunks WHERE embedding IS NULL LIMIT %s", (select_limit,))
        rows = cur.fetchall()
        conn.commit()  # close the read transaction before slow network embedding work
        if not rows:
            break
        print(f"  selected {len(rows)} pending chunks for embedding", flush=True)
        pairs = [rows[i:i + EMBED_BATCH_SIZE] for i in range(0, len(rows), EMBED_BATCH_SIZE)]
        results = []
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=EMBED_WORKERS)
        try:
            for res in ex.map(_embed_pairs, pairs):
                results.extend(res)
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            ex.shutdown(wait=True)
        wrote = len(results)
        if results:                                   # bulk write: psycopg3 pipelines executemany,
            cur.executemany(                          # ~one round-trip vs 2000 (the real bottleneck)
                "UPDATE chunks SET embedding=%s::vector WHERE id=%s",
                [(_vec(emb), cid) for cid, emb in results])
        conn.commit()
        done += wrote
        print(f"  embedded ~{done}/{pending} (+{wrote})", flush=True)
        if max_total and done >= max_total:
            break
        if EMBED_MAX_SECONDS and (time.monotonic() - start) >= EMBED_MAX_SECONDS:
            print(f"  time budget {EMBED_MAX_SECONDS:.0f}s reached — stopping "
                  f"(committed progress persists; next run resumes)", flush=True)
            break
        if wrote == 0:
            # Surface the ACTUAL reason (over-context after truncation, a parse
            # error, etc.) instead of blind-guessing "endpoint down" — the last
            # drop reason is set whenever a chunk is permanently dropped.
            reason = _LAST_DROP_REASON or "endpoint unreachable/ceded"
            print(f"  no progress ({reason}) — backing off 30s", flush=True)
            time.sleep(30)
    # build HNSW once vectors exist (idempotent)
    cur.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    if cur.fetchone()[0] > 0:
        print("ensuring HNSW index exists (cosine)...", flush=True)
        cur.execute("CREATE INDEX IF NOT EXISTS chunks_emb_hnsw ON chunks "
                    "USING hnsw (embedding vector_cosine_ops)")
        conn.commit()
    print("embed done.", flush=True)

# --- search -----------------------------------------------------------------

_TERM_RE = re.compile(r"[a-z0-9]+")
_QUERY_STOP_TERMS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "but", "by", "can", "did",
    "do", "does", "for", "from", "had", "has", "have", "how", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "our", "that", "the", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "why",
    "said", "say", "with", "you",
}
_SOFT_QUERY_STOP_TERMS = _QUERY_STOP_TERMS | {
    "archive", "find", "local", "look", "remember", "search", "semantic",
}

def _query_terms(q: str, limit: int = SEARCH_SOFT_TERM_LIMIT) -> list[str]:
    terms: list[str] = []
    seen = set()
    for term in _TERM_RE.findall(q.lower()):
        if term in _QUERY_STOP_TERMS or len(term) < 2:
            continue
        if term not in seen:
            terms.append(term)
            seen.add(term)
        if len(terms) >= limit:
            break
    return terms

def _soft_terms(q: str) -> list[str]:
    terms = [t for t in _query_terms(q) if t not in _SOFT_QUERY_STOP_TERMS]
    return terms or _query_terms(q)

def _should_exact_phrase(q: str) -> bool:
    return 0 < len(_query_terms(q, limit=SEARCH_EXACT_MAX_TERMS + 1)) <= SEARCH_EXACT_MAX_TERMS

def _rrf(rank: int, k: int = RRF_K) -> float:
    return 1.0 / (k + rank)

def _recency01(datestr: str) -> float:
    """Newer→1.0, older→0.0, linear over RECENCY_WINDOW_DAYS. 0.0 if unparseable."""
    try:
        t = time.mktime(time.strptime((datestr or "")[:10], "%Y-%m-%d"))
    except Exception:
        return 0.0
    days_ago = max(0.0, (time.time() - t) / 86400.0)
    return max(0.0, 1.0 - days_ago / RECENCY_WINDOW_DAYS)

def _fuse_ranked(results, limit: int):
    for x in results.values():
        score = x.get("kw_rrf", 0.0)
        if x.get("kw_rank") and not x.get("kw_rrf"):
            score += 1.6 * _rrf(x["kw_rank"])
        if x.get("sem_rank"):
            score += 1.0 * _rrf(x["sem_rank"])
        if x.get("exact"):
            score += 0.02
        # recency edge on the project record only (supersession signal)
        if x["r"][1] in _PROJECT_RECORD_KINDS and x["r"][5]:
            score += RECENCY_WEIGHT * _recency01(x["r"][5])
        x["score"] = score
    ranked = sorted(results.values(),
                    key=lambda x: (x.get("score", 0.0), x.get("sem", 0.0)),
                    reverse=True)
    out = []
    seen_sources = set()
    for x in ranked:
        source_id = str(x["r"][0]).rsplit("#", 1)[0]
        if source_id in seen_sources:
            continue
        seen_sources.add(source_id)
        out.append(x)
        if len(out) >= limit:
            break
    return out

def _add_keyword_result(results, row, rank: int, weight: float, exact: bool = False):
    rid = row[0]
    if rid not in results:
        results[rid] = {"r": row[:6], "kw": True, "sem": 0.0, "kwscore": 0.0}
    x = results[rid]
    x["kw"] = True
    x["kw_rrf"] = x.get("kw_rrf", 0.0) + weight * _rrf(rank)
    x["kw_rank"] = min(rank, x.get("kw_rank", rank))
    x["kwscore"] = max(float(row[6] or 0.0) if len(row) > 6 else 0.0, x.get("kwscore", 0.0))
    x["exact"] = bool(x.get("exact") or exact)

def _keyword_results(cur, q: str, where_scope: str, params_scope: list):
    results = {}
    if _should_exact_phrase(q):
        cur.execute(
            f"""SELECT id,source_kind,project,title,left(text,200),created,1.0 AS rank
                FROM chunks
                WHERE text ILIKE %s{where_scope}
                ORDER BY length(text) ASC
                LIMIT %s""",
            [f"%{q}%", *params_scope, min(SEARCH_KEYWORD_LIMIT, 25)])
        for rank, r in enumerate(cur.fetchall(), 1):
            _add_keyword_result(results, r, rank, weight=2.4, exact=True)

    cur.execute(
        f"""SELECT id,source_kind,project,title,left(text,200),created,
                   ts_rank(tsv, plainto_tsquery('simple',%s)) AS rank
            FROM chunks
            WHERE tsv @@ plainto_tsquery('simple',%s){where_scope}
            ORDER BY ts_rank(tsv, plainto_tsquery('simple',%s)) DESC
            LIMIT %s""",
        [q, q, *params_scope, q, SEARCH_KEYWORD_LIMIT])
    for rank, r in enumerate(cur.fetchall(), 1):
        _add_keyword_result(results, r, rank, weight=1.6)

    for term in _soft_terms(q):
        cur.execute(
            f"""SELECT id,source_kind,project,title,left(text,200),created,
                       ts_rank(tsv, to_tsquery('simple',%s)) AS rank
                FROM chunks
                WHERE tsv @@ to_tsquery('simple',%s){where_scope}
                ORDER BY ts_rank(tsv, to_tsquery('simple',%s)) DESC
                LIMIT %s""",
            [term, term, *params_scope, term, SEARCH_SOFT_PER_TERM_LIMIT])
        for rank, r in enumerate(cur.fetchall(), 1):
            _add_keyword_result(results, r, rank, weight=0.35)
    return results

def search_results(q: str, scope=None, projects=None, keyword=False, limit=12, conn=None):
    owned = conn is None
    conn = conn or load_conn()
    cur = conn.cursor()
    # Two independent hard filters, both threaded through the existing
    # where_scope/params_scope seam (kept in placeholder order): source_kind
    # (which corpus) AND project (which repo/project). project scoping is what
    # makes the cross-project index usable — restrict "was this tried in THIS
    # repo?" to the repo, instead of leaking cross-project noise.
    where_parts, params_scope = [], []
    if scope:
        where_parts.append(" AND source_kind = ANY(%s)")
        params_scope.append(scope)
    if projects:
        where_parts.append(" AND project = ANY(%s)")
        params_scope.append(projects)
    where_scope = "".join(where_parts)
    # 1) keyword layer, no GPU. Exact phrase is intentionally limited to short
    #    queries; long natural-language queries are better served by FTS layers.
    results = _keyword_results(cur, q, where_scope, params_scope)
    # 2) semantic layer (if embeddings exist + embedder reachable)
    if not keyword:
        try:
            qe = _vec(_embed_batch([_embed_query_text(q)])[0])
            try:
                cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
            except Exception:
                pass
            cur.execute(
                f"""SELECT id,source_kind,project,title,left(text,200),created,
                           1-(embedding<=>%s::vector) AS sem FROM chunks
                    WHERE embedding IS NOT NULL{where_scope}
                    ORDER BY embedding<=>%s::vector LIMIT %s""",
                [qe, *params_scope, qe, SEARCH_SEMANTIC_LIMIT])
            for rank, r in enumerate(cur.fetchall(), 1):
                rid = r[0]
                if rid in results:
                    results[rid]["sem"] = float(r[6])
                    results[rid]["sem_rank"] = rank
                else:
                    results[rid] = {"r": r[:6], "kw": False, "sem": float(r[6]),
                                    "kwscore": 0.0, "sem_rank": rank}
        except Exception as e:
            print(f"(semantic layer skipped: {e})", file=sys.stderr)
    ranked = _fuse_ranked(results, limit)
    if owned:
        conn.close()
    return ranked

def cmd_search(args):
    q = " ".join(args.query)
    scope = _scope(args)
    projects = _project_scope(args)
    ranked = search_results(q, scope=scope, projects=projects,
                            keyword=args.keyword, limit=args.limit)
    if not ranked:
        print("No results."); return
    tag = {"session": "\033[36m[session]", "doc": "\033[33m[doc]", "messenger": "\033[35m[msgr]",
           "aichat": "\033[34m[aichat]", "note": "\033[32m[note]", "email": "\033[90m[email]",
           "things3": "\033[93m[things3]", "fyr": "\033[91m[fyr]",
           "screenshot": "\033[96m[shot]", "git_commit": "\033[92m[commit]",
           "code": "\033[94m[code]"}
    for i, x in enumerate(ranked, 1):
        r = x["r"]
        kw = " \033[32m[kw]\033[0m" if x["kw"] else ""
        # Date is a BRIGHT visual anchor (not buried in dim parens): on a
        # flip-flopped topic the reader must instantly see which hit is current.
        date = f" \033[1;33m{r[5]}\033[0m" if r[5] else ""
        print(f"{i}. {tag.get(r[1],'[?]')}\033[0m \033[1m{r[3]}\033[0m{date}  "
              f"\033[2m({r[2]}, sem={x['sem']:.2f})\033[0m{kw}")
        print(f"   \033[2m{(r[4] or '').replace(chr(10),' ')[:180]}\033[0m")

def _result_blob(result) -> str:
    return " ".join(str(x or "") for x in result["r"]).lower()

def _case_scope(case):
    scope = case.get("scope")
    if isinstance(scope, str):
        return [scope]
    if isinstance(scope, list):
        return scope
    return None

def _case_projects(case):
    proj = case.get("project") or case.get("repo")
    if isinstance(proj, str):
        return [proj]
    if isinstance(proj, list):
        return proj
    return None

def _case_expectations(case):
    exp = case.get("must_match") or case.get("expected") or []
    if isinstance(exp, (str, dict)):
        return [exp]
    return exp

def _expectation_matches(result, expected) -> bool:
    blob = _result_blob(result)
    if isinstance(expected, str):
        return expected.lower() in blob
    if isinstance(expected, dict):
        checks = []
        if expected.get("contains"):
            checks.append(str(expected["contains"]).lower() in blob)
        field_map = {"id": 0, "source_kind": 1, "project": 2, "title": 3, "text": 4, "created": 5}
        for key, idx in field_map.items():
            if expected.get(key):
                checks.append(str(expected[key]).lower() in str(result["r"][idx] or "").lower())
        return bool(checks) and all(checks)
    return False

def _hit_rank(results, expectations):
    for i, r in enumerate(results, 1):
        if any(_expectation_matches(r, e) for e in expectations):
            return i
    return None

def _pctl(values, pct: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * pct))))
    return vals[idx]

def cmd_eval(args):
    path = Path(args.file)
    cases = json.loads(path.read_text(encoding="utf-8"))
    conn = load_conn()
    rows = []
    hits = 0
    rr_sum = 0.0
    latencies = []
    for case in cases:
        q = case["query"]
        expectations = _case_expectations(case)
        t0 = time.perf_counter()
        ranked = search_results(q, scope=_case_scope(case), projects=_case_projects(case),
                                keyword=args.keyword, limit=args.k, conn=conn)
        latency_ms = (time.perf_counter() - t0) * 1000
        latencies.append(latency_ms)
        rank = _hit_rank(ranked, expectations)
        hit = rank is not None
        hits += 1 if hit else 0
        rr_sum += (1.0 / rank) if rank else 0.0
        rows.append({"query": q, "hit": hit, "rank": rank,
                     "latency_ms": round(latency_ms, 1),
                     "top": [r["r"][3] for r in ranked[:3]]})
    conn.close()
    summary = {"cases": len(cases), f"recall@{args.k}": hits / len(cases) if cases else 0.0,
               "mrr": rr_sum / len(cases) if cases else 0.0,
               "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
               "latency_ms_p95": round(_pctl(latencies, 0.95), 1),
               "rows": rows}
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"eval: {summary['cases']} cases  recall@{args.k}={summary[f'recall@{args.k}']:.2f}  "
          f"mrr={summary['mrr']:.2f}  avg={summary['latency_ms_avg']:.1f}ms  "
          f"p95={summary['latency_ms_p95']:.1f}ms")
    for row in rows:
        mark = "ok" if row["hit"] else "MISS"
        rank = row["rank"] if row["rank"] is not None else "-"
        print(f"  {mark:4} rank={rank:>2} {row['latency_ms']:>7.1f}ms  {row['query']}")
        if args.show_top or not row["hit"]:
            for title in row["top"]:
                print(f"       top: {title}")

def cmd_stats(args):
    conn = load_conn(); cur = conn.cursor()
    cur.execute("SELECT source_kind,count(*),count(embedding) FROM chunks GROUP BY source_kind ORDER BY 2 DESC")
    print("mannaminne (Postgres+pgvector on Darwin) — chunk counts:")
    tot = emb = 0
    for k, c, e in cur.fetchall():
        print(f"  {k:10} {c:>8} chunks  ({e} embedded)"); tot += c; emb += e
    print(f"  {'TOTAL':10} {tot:>8} chunks  ({emb} embedded, {tot-emb} pending)")

# --- scope / cli ------------------------------------------------------------

def _scope(args):
    flags = []
    for k in ("session", "doc", "messenger", "aichat", "note", "email",
              "things3", "fyr", "screenshot", "git_commit", "code"):
        if getattr(args, k, False):
            flags.append(k)
    if flags:
        return flags
    invoked = os.environ.get("MANNAMINNE_INVOKED_AS") or os.path.basename(sys.argv[0])
    if invoked == "ccsearch":
        return ["session", "doc", "git_commit", "code"]   # ccsearch alias → code-project sources
    return None                     # mannaminne / minne → all sources

def _project_scope(args):
    """Hard project/repo filter (WHERE project = ANY). `--project brf-auto` or
    `--project a b c`. Empty → no project filter (all projects)."""
    proj = getattr(args, "project", None)
    return [p for p in proj if p] if proj else None

def _add_search_args(sp):
    sp.add_argument("query", nargs="*")
    sp.add_argument("-k", "--keyword", action="store_true")
    sp.add_argument("-n", "--limit", type=int, default=12)
    sp.add_argument("-s", "--session", action="store_true")
    sp.add_argument("-d", "--doc", action="store_true")
    sp.add_argument("-m", "--messenger", action="store_true")
    sp.add_argument("-a", "--aichat", action="store_true")
    sp.add_argument("--note", action="store_true")
    sp.add_argument("-e", "--email", action="store_true")
    sp.add_argument("-t", "--things3", action="store_true")
    sp.add_argument("-f", "--fyr", action="store_true")
    sp.add_argument("-p", "--photos", dest="screenshot", action="store_true")
    sp.add_argument("-g", "--git", dest="git_commit", action="store_true")
    sp.add_argument("-c", "--code", action="store_true")
    sp.add_argument("-P", "--project", "--repo", nargs="*",
                    help="restrict to project/repo(s), e.g. --project brf-auto")

def main():
    argv = sys.argv[1:]
    cmds = {"ingest", "embed", "stats", "search", "eval"}
    if not argv or argv[0] not in cmds:
        sp = argparse.ArgumentParser(prog="mannaminne")
        _add_search_args(sp)
        a = sp.parse_args(argv)
        if not a.query:
            print("usage: mannaminne <query> | ingest [--sources ...] | embed | search [-smdan] | stats")
            return
        cmd_search(a)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "ingest":
        sp = argparse.ArgumentParser(); sp.add_argument("--sources", nargs="*")
        cmd_ingest(sp.parse_args(rest))
    elif cmd == "embed":
        sp = argparse.ArgumentParser()
        sp.add_argument("--limit", type=int, default=0,
                        help="maximum pending chunks to embed in this run")
        cmd_embed(sp.parse_args(rest))
    elif cmd == "stats":
        cmd_stats(None)
    elif cmd == "search":
        sp = argparse.ArgumentParser(); _add_search_args(sp)
        cmd_search(sp.parse_args(rest))
    elif cmd == "eval":
        sp = argparse.ArgumentParser()
        sp.add_argument("--file", default=str(Path(__file__).resolve().parents[1] / "eval/golden_queries.json"))
        sp.add_argument("-k", type=int, default=10)
        sp.add_argument("--keyword", action="store_true")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--show-top", action="store_true")
        cmd_eval(sp.parse_args(rest))

if __name__ == "__main__":
    main()
