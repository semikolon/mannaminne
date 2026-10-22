# mannaminne

**Local search over one person's whole digital archive: notes, documents, email, chat history,
AI-chat exports, source code, git history and the transcripts of AI coding sessions. Keyword
search and semantic search combined in Postgres, with embeddings computed on my own hardware.**

*mannaminne* is Swedish for living memory, as in *i mannaminne*, "within living memory".

## Why it exists

I run most of my life through AI agents, mainly Claude Code and sometimes Codex. Software is one
part of it. The rest is everything else: planning a move and deciding what to keep, job
applications and letters, money and paperwork, and long conversations about concepts, ideas and
philosophy for idea-heavy projects such as Deliberus. Every session starts with no memory of the
one before, while what matters is already written down somewhere in years of notes, documents,
mail, messages and code.

mannaminne lets an agent, or me, find it by what it was about rather than by guessing its exact
words, in Swedish or in English, in a second or two. That is why it holds mail and messages as
well as code. Before an agent writes a letter in my name it can check what was actually said,
before it gives advice it can find what I already decided, and before it calls an idea new it
can see whether it was tried. The agents' standing instructions tell them to search before they
claim that something is new, was never tried, or does not exist.

## What it indexes

| Kind | What |
|---|---|
| `email` | a live Gmail account, plus older mailbox archives |
| `messenger` | Facebook Messenger history |
| `aichat` | ChatGPT and Claude conversation exports |
| `note` | Simplenote |
| `doc` | Markdown and text documents in project folders and notes |
| `session` | Claude Code transcripts, with tool output and injected instructions filtered out |
| `said` | only what I typed into Claude Code, from the transcripts and, where a transcript is gone, from Claude Code's prompt history |
| `code` | source files from about forty repositories, chunked along the syntax tree |
| `git_commit` | the commit messages of the same repositories |
| `things3`, `fyr` | task lists |
| `fbgroup` | the Facebook group where Deliberus was first discussed, 2011 to 2013: 406 posts with their comments, scraped once in March 2026; a post is dated to the latest year Facebook's relative age allows |
| `screenshot` | text read from screenshots with Apple's on-device OCR; a screenshot deleted from Photos keeps its text, read from its original on VOLTA (`docs/how-it-works.md`, *Gone means gone from the copy that is kept*) |

`mannaminne stats` prints the current size and how much of it has embeddings. In October 2026
it was about 2.35 million chunks, four fifths of them mail.

## How it works

- **Ingest** runs every night. It reads each source, cuts the text into passages of about a
  paragraph, and stores them in one Postgres table. Only what changed since the last run is read
  again, and code is cut along functions and classes rather than at a fixed length.
- **Embed** turns each passage into a vector on a GPU in a home server. Nothing is sent to a
  cloud service.
- **Search** runs a keyword search and a search by meaning side by side and merges the two lists,
  so an exact name and a vague description both find their passage. Each hit shows the lines
  around the match, its date and where it came from.

## Design choices

- **Keyword search always works, meaning is added on top.** A new passage can be found by its
  words at once, before any GPU time is spent on it.
- **One database for both kinds of search,** so there is nothing to keep in sync, and it holds
  millions of passages. Mail, four fifths of them, sits in its own part, so ordinary searches stay
  in memory.
- **Swedish and English both work.** Swedish compound words are matched by their beginnings, so
  *möte* also finds *mötesprotokoll*.
- **Newer wins in project notes, not in life.** A newer note outranks an older one on the same
  topic, while old mail and messages are never pushed down for their age.
- **It is measured.** `mannaminne eval` reports how often the right answer comes first on fixed
  sets of questions, and changes are checked against it.

The details, and what each choice taught along the way: [`docs/how-it-works.md`](docs/how-it-works.md).

## What else reads the index

Two of the owner's other tools read this database directly, so a change to these shapes is a
change to them too:

| Reader | Reads | If the shape changes |
|---|---|---|
| `natverk`, a private register of the people the owner knows | the first chunk of every mail (its `From:`, `To:` and `Cc:` lines and date), `email_class.class`, and document chunks by `source_id` (`doc:<first folder>:<path>`) in `chunk_idx` order | its build refuses a reading that holds less than half of the last one and keeps the old one, so the break shows in `natverk --check` rather than as missing people |
| `job-watch` | `py/inbox_assignments.py --json --check` | the daily job-ad push stops finding broker mail |

## Usage

```bash
mannaminne "when did we decide to drop the MCP server"    # everything except mail
mannaminne -e "the lease we signed in 2019"                # mail only
mannaminne -A "the lease we signed in 2019"                # everything, mail included
mannaminne -d "chunk size for code"                        # documents only
mannaminne -c -g "retry logic" -P myrepo                   # code and commits in one repository
mannaminne -k "an exact phrase"                            # keyword layers only, no GPU
mannaminne stats                                           # size and embedding coverage
mannaminne eval                                            # recall, MRR and latency on a query set
mannaminne ingest                                          # discover, chunk and upsert all sources
mannaminne embed --limit 500                               # fill missing embeddings
```

A plain search leaves mail out. Mail is four fifths of the rows and is kept in its own part of the
table, so leaving it out keeps a search in the part that fits in memory; `-e` searches mail and
`-A` (also `--all`) searches everything.

Source flags narrow a search to one or more kinds: `-s` sessions, `-d` docs, `-m` Messenger,
`-a` AI chats, `--note`, `-e` email, `-t` Things 3, `-f` Fyr, `-p` screenshots, `-g` commits,
`-c` code, `--fbgroup` the Facebook group, `--said` what I typed in Claude Code (left out of a plain search, since the session
rows hold the same words). `-P` (also `--repo`) narrows to named projects; put it last, since it takes several
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
| `py/corpus_mail.py`, `py/inbox_*.py`, `py/mail_judge.py` | read-only tools for working through a large mail archive: whole messages from chunks, senders, and which bulk mail can skip the embedding queue. The mail judge pays from `MANNAMINNE_OPENROUTER_API_KEY`, falling back to the shared `OPENROUTER_API_KEY` (one OpenRouter key per project, 2026-10-18) |
| `py/gmail_sweep.py`, `py/gmail_backfill.py` | moving bulk mail out of Gmail only after a checked copy is on disk and in the index, and indexing mail from before a cut-off date |
| `py/tests/` | unit tests, plus the tests against the live index, which skip when it is unreachable |
| `eval/` | the query-set format and how a set is chosen |
| `z4/` | scripts for bulk embedding on a borrowed Windows workstation, with a guard that stops when its owner starts working |
| `src/` | the first version, in Rust, kept for its history |

```bash
cd py && .venv/bin/python -m unittest discover -s tests -v
```
