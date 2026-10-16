# mannaminne

**Local search over one person's whole digital archive: notes, documents, email, chat history,
AI-chat exports, source code, git history and the transcripts of AI coding sessions. Keyword
search and semantic search combined in Postgres, with embeddings computed on my own hardware.**

*mannaminne* is Swedish for living memory, as in *i mannaminne*, "within living memory".

## Why it exists

I build most of my software together with AI coding agents. Every session starts with no memory
of the one before, while the answers to the questions that matter most (*was this tried before?
what did we decide, and why? where did I write about this?*) already sit somewhere in years of
documents, commits, conversations and mail. Grep finds a passage if you guess its exact words.
mannaminne finds it when you only remember what it was about, in Swedish or in English, and
answers in a few seconds.

I and my agents use it the same way, as a command-line tool. The agents' standing instructions
tell them to search it before they claim that something is new, was never tried, or does not
exist.

## What it indexes

| Kind | What |
|---|---|
| `email` | a live Gmail account, plus older mailbox archives |
| `messenger` | Facebook Messenger history |
| `aichat` | ChatGPT and Claude conversation exports |
| `note` | Simplenote |
| `doc` | Markdown and text documents in project folders and notes |
| `session` | Claude Code transcripts, with tool output and injected instructions filtered out |
| `code` | source files from about forty repositories, chunked along the syntax tree |
| `git_commit` | the commit messages of the same repositories |
| `things3`, `fyr` | task lists |
| `screenshot` | text read from screenshots with Apple's on-device OCR |

`mannaminne stats` prints the current size and how much of it has embeddings. In October 2026
it was about 2.35 million chunks, four fifths of them mail.

## How it works

```
sources ─► discover ─► chunk ─► Postgres: one table ─► search
                                  ├─ text + tsvector        (keyword)
                                  └─ pgvector, HNSW index   (meaning)
                                        ▲
                        embed on a local GPU
```

**Ingest.** Each source has a small discoverer that yields documents. A document is cut into
chunks of about 750 characters that overlap by 80, so a phrase on a boundary is still found.
Markdown is cut at its headings. Code is cut along its syntax tree with tree-sitter, using the
cAST method: small sibling nodes are merged and large ones split, so a function that fits stays
whole, and each chunk starts with its file path and symbol name so the vector knows where it
came from. A commit message is one chunk. Every chunk carries a hash of its text, so a re-run
rewrites only what changed and re-embeds only that. A file whose size and modification time are
unchanged is not read at all.

**Store.** One `chunks` table in Postgres 16 with pgvector. A generated `tsvector` column and a
trigram index serve keyword search, and an HNSW index over 1024-dimensional vectors serves
semantic search. The text, its keyword index and its vector live in the same row, so a hybrid
query needs one database and nothing to keep in sync.

**Embed.** Qwen3-Embedding-4B, served through llama.cpp's OpenAI-compatible endpoint from a
GTX 1650 in a home server. Large backfills run on a borrowed workstation GPU when its owner is
not using it (`z4/`). Nothing is sent to a cloud service. A query gets an instruction prefix;
stored chunks are embedded as plain text. A chunk too dense for the server's context window is
truncated and retried rather than dropped, and a batch that fails is split until the one bad
item is isolated.

**Search.** Keyword passes run first: an exact-phrase match for short queries, a search on all
the terms, and a small probe per term. A vector search runs beside them. The ranked lists are
merged with reciprocal rank fusion and de-duplicated per document, so one long conversation
cannot fill the page. Each hit shows a snippet centred on the match. When part of the searched
scope has no embeddings yet, the search says how much.

## Design choices, and what they taught

- **Keyword search is the guarantee, meaning is the bonus.** The requirement was to find any
  needle in the haystack. Full-text search finds it the moment a chunk is ingested, before any
  GPU time is spent, and semantic search fills in as the vectors arrive. That is how the index
  grew to millions of chunks on a small GPU without ever being blind.
- **Postgres instead of a separate vector database.** The first version, a small Rust tool from
  March 2026 (`src/`), kept its index in memory and embedded only the first 800 or so characters
  of each item. The rebuild on Postgres in June gave full coverage, one store for both kinds of
  search, and an index that does not have to fit in memory.
- **Swedish needs its own care.** About half the archive is Swedish. The keyword index uses
  Postgres' `simple` configuration, without stemming: the Swedish stemmer joined one of four real
  word pairs it was tested on and mangled the English half of the corpus (*files* became *fil*).
  Swedish builds meaning by compounding (*möte*, *mötesprotokoll*), so terms of four letters or
  more are also matched as prefixes, which doubled the hits for a word like *möte*.
- **Test against the real index, not only against fixtures.** An early tokenizer pattern quietly
  split every word containing å, ä or ö, and ranking every match of a common word made long
  queries take close to a minute. Both passed the unit tests, which were written in English and
  ran on a handful of rows. Two tests now run against the live index: a Swedish word must match,
  and a long Swedish query must stay under a time limit. Capping the candidate set took that
  query from about 50 seconds to under two.
- **A snippet is what the reader decides from.** The first 200 characters of a chunk often had
  nothing to do with why it matched. Snippets are now centred on the match, at the same cost.
- **Newer wins on the project record, not in life.** For documents, sessions, commits and code, a
  small recency term lets a newer note outrank an older one on the same topic that it replaced.
  Mail and messages get no such term; a letter from 2012 should stay as findable as one from
  last week.
- **Measure before adding cleverness.** A reranker, GraphRAG and query rewriting were each weighed
  against published results and against this corpus. Query rewriting hurts retrieval when the
  query already uses the corpus's own words, and it costs a model call before every search.
  GraphRAG suits corpora with a few clear kinds of entity, and sixteen years of mail, chat and
  code has no such shape. A reranker is worth adding once a Swedish evaluation set can say which
  one, and `mannaminne eval` exists for that.

## Usage

```bash
mannaminne "when did we decide to drop the MCP server"    # search everything
mannaminne -d "chunk size for code"                        # documents only
mannaminne -c -g "retry logic" -P myrepo                   # code and commits in one repository
mannaminne -k "an exact phrase"                            # keyword layers only, no GPU
mannaminne stats                                           # size and embedding coverage
mannaminne eval                                            # recall, MRR and latency on a query set
mannaminne ingest                                          # discover, chunk and upsert all sources
mannaminne embed --limit 500                               # fill missing embeddings
```

Source flags narrow a search to one or more kinds: `-s` sessions, `-d` docs, `-m` Messenger,
`-a` AI chats, `--note`, `-e` email, `-t` Things 3, `-f` Fyr, `-p` screenshots, `-g` commits,
`-c` code. `-P` (also `--repo`) narrows to named projects; put it last, since it takes several
names. Invoked as `ccsearch`, the tool searches sessions, docs, commits and code by default.

## Running it yourself

`setup.sh` provisions my own machines (a home server reached over ssh) and is best read as a
recipe. What it needs:

- Python 3 with `psycopg` and, for the code chunker, `tree-sitter` with
  `tree-sitter-language-pack`
- Postgres 16 with the `vector` and `pg_trgm` extensions, and the `chunks` table from `setup.sh`
- an OpenAI-compatible embeddings endpoint serving a model with 1024-dimensional output, set with
  `MANNAMINNE_EMBED_URL`
- the connection in `~/.config/mannaminne/db.env` (`MANNAMINNE_PG_HOST`, `_PORT`, `_DB`, `_USER`,
  `_PASSWORD`)

Where each source is read from is set in its `discover_*` function in `py/mannaminne.py`. Most
of those paths point at my own machines and need editing; a few can be set from the environment
(`MANNAMINNE_SESSION_PATHS`, `MANNAMINNE_GIT_REPOS`).

Lists that describe one person's data stay outside the repository, in `~/.config/mannaminne/`:
the evaluation query sets, the list of repositories to index, and the rules that class mail by
sender. `eval/example_queries.json` shows the query-set format with invented questions.

## Repository layout

| Path | What |
|---|---|
| `py/mannaminne.py` | the CLI: ingest, embed, search, stats, eval |
| `py/code_chunker.py` | the cAST chunker over tree-sitter |
| `py/corpus_mail.py`, `py/inbox_*.py`, `py/mail_judge.py` | read-only tools for working through a large mail archive: whole messages from chunks, senders, and which bulk mail can skip the embedding queue |
| `py/tests/` | unit tests, plus the tests against the live index, which skip when it is unreachable |
| `eval/` | the query-set format and how a set is chosen |
| `z4/` | scripts for bulk embedding on a borrowed Windows workstation, with a guard that stops when its owner starts working |
| `src/` | the first version, in Rust, kept for its history |

```bash
cd py && .venv/bin/python -m unittest discover -s tests -v
```
