# mannaminne

Hybrid semantic + keyword search over Fredrik's personal life-corpus: Claude
Code sessions, local docs, Facebook Messenger, AI-chat archives, Simplenote,
email, Things3, Fyr tasks, screenshots, and photo labels/OCR.

`mannaminne` means "in living memory". The old Rust binary remains in the repo
for history, but the active implementation is:

- Python CLI: `py/mannaminne.py`
- Store: dedicated Postgres + pgvector on Darwin
- Keyword: Postgres FTS + trigram substring
- Semantic: Qwen3-Embedding-4B vectors, HNSW index

## Usage

```bash
mannaminne "kausal inferens Kaus"
mannaminne search -d "Z4 embedder backlog HNSW"
mannaminne search --keyword "exact phrase"
mannaminne stats
mannaminne ingest --sources doc
mannaminne embed
mannaminne embed --limit 500
mannaminne eval --show-top
```

Aliases:

| Invoke as | Scope |
|---|---|
| `mannaminne` / `minne` | all sources |
| `ccsearch` | legacy CC scope: sessions + docs |

Source flags override the invocation default:

```bash
-s / --session
-d / --doc
-m / --messenger
-a / --aichat
--note
-e / --email
-t / --things3
-f / --fyr
-p / --photos
```

## Embedding Endpoint Selection

If `MANNAMINNE_EMBED_URL` is set, it is used exactly.

Otherwise runtime default order is:

1. Z4 tunnel: `MANNAMINNE_Z4_EMBED_URL`, default `http://127.0.0.1:8081/v1/embeddings`
2. Darwin fallback: `MANNAMINNE_DARWIN_EMBED_URL`, default `http://192.168.4.1:8080/v1/embeddings`

Darwin-safe defaults are intentionally conservative:

```bash
MANNAMINNE_EMBED_BATCH_SIZE=4
MANNAMINNE_EMBED_WORKERS=2
MANNAMINNE_EMBED_SELECT_LIMIT=500
MANNAMINNE_EMBED_TIMEOUT=45
MANNAMINNE_EMBED_PROBE_TIMEOUT=5
```

For a Z4 batch run, override these upward after the Z4 server/tunnel is live.

## The email index is NOT a view of your Gmail account

It is the union of the live account and the mbox archives in `MBOX_SOURCES`, and
on this corpus the archive is much the larger half: the single biggest sender has
**140 572 messages in the FERMI archive and 95 in Gmail**. Anyone reasoning about
"the email corpus" as though it were the inbox will be wrong by that margin, and
`project` cannot tell them apart — the ingest hardcodes `"gmail"` for both.

Two things follow. **Deleting mail from Gmail does not remove it here**: email is
marked `PARTIAL` on every ingest and excluded from the orphan prune by name, so
chunks outlive their messages. And **corpus volume is the wrong ranking for any
storage decision**, since a sender can be enormous here and absent there.

Full measurement + the inbox-cleanup arc:
`~/dotfiles/docs/gmail_storage_and_index_pruning_2026-09-06.md`.

## Indexing

`ingest` discovers source content, chunks it, and upserts rows. If text changes,
the chunk's embedding is reset to `NULL` so `embed` can refill it.

**Unchanged files are skipped per FILE, not per kind** (2026-09-05). A fingerprint of
`mtime_ns:size` gates the five discoverers that walk files — docs, sessions, code, git
commits, screenshots — and a skipped file's `source_id` is carried into the `_seen`
anti-join so the prune does not delete chunks whose file was never read. Fingerprints
are saved only when a kind completes, so an interrupted run re-reads rather than
silently skipping, and a failed carry-forward drops that kind from the prune entirely
rather than risking deletion. Before this, `_skip_if_unchanged` worked per KIND, so one
edited file re-chunked the whole kind: ~295 000 chunks re-upserted nightly for ~1 500
new ones, and a run that occupied 05:00–11:00 every day
(`docs/nightly_ingest_cost_2026-09-05.md`). The LaunchAgent also runs at background
priority now, so a long run yields to whoever is at the keyboard.

Docs use heading-aware markdown chunks. The global
`~/.claude/CLAUDE.md` file is indexed as a special doc source because it contains
high-value operating context outside the usual docs roots.

`embed` fills `NULL` embeddings. Failed batches are split recursively, so one bad
batch does not poison the whole pending set. Use `embed --limit N` for bounded
smoke tests or staged backfills.

**Scheduled (Mac Mini):** two launchd jobs run nightly — **except that the embed drainer is `launchctl disable`d since 2026-09-05** so it does not grind the same queue as the Z4 backfill; re-enable (`launchctl enable` + `bootstrap`) once the Z4 has drained the queue, or new chunks stop being embedded. The jobs: a **02:00 embed-only
backlog drainer** (`mannaminne-scheduled-embed`, ~3h budget, quiet-hour so it
never contends with daytime embedder use) and the **05:00 full ingest+embed**
(`mannaminne-ingest-runner` → `mannaminne-scheduled-ingest`, ~1h embed budget,
runs after the 04:00 nightly-sweep that archives CC sessions >90d to FERMI). Both
are budget-capped and resume from committed progress. Deployment detail (the
nit-tracked plists, the FDA-granted responsible-process runner that reads
~/Documents + FERMI with no TCC dialog, the FERMI-mount guard) lives in global
`~/.claude/CLAUDE.md` § mannaminne.

## Search Ranking

Search is hybrid:

1. Keyword candidates from layered Postgres FTS:
   exact phrase for short queries, strict all-term FTS, then small per-term
   recall probes for useful non-filler terms.
2. Semantic candidates from pgvector.
3. Reciprocal-rank fusion combines the layers, with a small exact-match boost.
4. Final results are de-duplicated by source object so one long conversation or
   doc cannot fill the whole first page with adjacent chunks.

The query embedding uses a Qwen3 instruction prefix; stored document chunks stay
plain text.

## Eval

Golden queries live in `eval/golden_queries.json`.

```bash
mannaminne eval
mannaminne eval --keyword
mannaminne eval -k 20 --json
```

The eval command reports recall@k, MRR, average latency, and p95 latency against
the live DB. It is meant as a small regression harness before changing chunking,
fusion, model, or storage settings.

## Tests

Tests use Python's standard `unittest`, no pytest dependency:

```bash
cd ~/Projects/mannaminne/py
.venv/bin/python -m unittest discover -s tests -v
```

Current tests cover:

- char and markdown chunking
- NUL stripping and chunk hashes
- Z4-first endpoint preference with Darwin fallback
- recursive embedding batch split
- rank fusion
- query term pruning
- source de-duplication
- eval expectation matching

## Setup

Fresh provisioning:

```bash
cd ~/Projects/mannaminne
./setup.sh
```

This creates the venv, wrappers in `~/.local/bin`, and the Darwin pgvector
Postgres container/schema. DB credentials live only in
`~/.config/mannaminne/db.env`.

Design and operational notes:

- `~/dotfiles/docs/personal_archives_semantic_search_2026_06_10.md`
- `~/Projects/mannaminne/z4/README.md`
- `~/dotfiles/docs/local_codebase_semantic_search_research_plan_2026_06_14.md`

## Embedding chosen projects first

`mannaminne embed --project deliberus --kind doc` embeds only that project's note chunks and stops when
they are done. Both flags take several names. The nightly ingest runs this for every project named in
`~/.config/mannaminne/embed_first.txt` (at most 30 minutes), and then for every project's notes, code, commits
and sessions (at most 30 more), whether or not the bulk queue is paused, so
that search by meaning does not go blind on the notes and code in daily use while an email backlog waits.
It did from 2026-09-05 to 2026-10-09: the pause meant for the backlog stopped everything.

## A search says when it is partial

A search by meaning can only find chunks that have an embedding. When part of what a search covered has none, it
prints one line on stderr with the count for each kind, for exactly the scope searched (`-d -P deliberus` counts
that project's notes). The count is made live beside the search, on its own connection, so nothing is cached and
the output on stdout is unchanged. `-k` searches by words alone and prints no such line.
