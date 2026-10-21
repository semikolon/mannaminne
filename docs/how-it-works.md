# How mannaminne works, in detail

*The README keeps the short version. This is the longer one, with the reasoning behind each choice and
what it taught. Moved out of the README on 2026-10-17.*

## The pipeline

```
sources ─► discover ─► chunk ─► Postgres: one table, mail in its own part ─► search
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

**Store.** One `chunks` table in Postgres 16 with pgvector, partitioned by source: mail, four
fifths of the rows, sits in `chunks_mail` and everything else in `chunks_rest`, each with its own
indexes. A plain search leaves mail out (`-e` searches mail only, `-A` everything), so it walks a
part small enough to stay in memory. A generated `tsvector` column and a
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
- **It only forgets what it has proved is gone.** A nightly run deletes chunks it did not see, so
  a skipped file counts as seen, a source on an unplugged disk raises an error instead of looking
  empty, and mail is never deleted.
- **Gone means gone from the copy that is kept, and only where one is declared** (2026-10-21).
  Screenshots are deleted from Photos to free iCloud while their originals and Photos records stay
  on VOLTA (`.osxphotos_export.db`), so a screenshot that has left the library is read from VOLTA
  with the same id and text, and FERMI or VOLTA being away skips the kind for the night. This is
  not a rule that a vanished source keeps its rows. Elsewhere disappearance usually means the row
  should go: a moved or renamed document would otherwise appear twice, and a file deleted because
  it was wrong would linger. Three consequences of the screenshot case: to make the index forget a
  screenshot, its file has to leave VOLTA too (restic still holds it); a screenshot put back into
  Photos gets a new id, so it is found twice until the old record is removed; and ordinary photos
  have no copy on VOLTA, so deleting one from Photos still drops its labels from the index.
  `tests/test_screenshot_archive_rows.py` pins it.
- **When unsure, keep.** Mail no rule places is sorted by a small model that sees only sender and
  subject. Written rules always win, and mail the model is less than 80 % sure about stays
  unsorted. The cleanup tools refuse to run without the list of senders to protect: if that list
  were empty by mistake, every mail would look unprotected and nothing would say so.
- **A pause must not starve what is used daily.** Embedding the mail backlog was paused to spare the
  home server, and for a month the pause also stopped new notes and code from being embedded.
  Notes, code and chosen projects are now embedded every night regardless.
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


## Search speed on a corpus larger than memory (2026-10-16)

By October 2026 the database had grown to about 27 GB on a server with 15 GB of memory, so a search
often read from disk, and the same query could take one second or nine. Measured that evening:

- **The embedding server was not the cause.** It was idle and embedded a query in 0.05 s.
- **The per-word keyword probes were.** They took 42 of the 46 seconds a thirteen-question evaluation
  spent in the database. At Postgres' default 4 MB of working memory, a search for a common word went
  lossy and rechecked whole pages; at 64 MB the same search touched a quarter as many pages, and the
  average search fell from 5.5 s to 1.5 s with the same answers. Search now sets 64 MB for its own
  transaction.
- **A scoped search lost candidates.** The vector index is searched first and filtered afterwards, so a
  search limited to documents, which are about 5 % of the corpus, kept 55 of its 100 candidates.
  pgvector's iterative scan keeps searching until the limit is met; with it the one question the
  evaluation missed was found again.
- **Tried and dropped:** skipping very common words entirely, judged by the planner's own row
  estimates. It saved another half second and lost a correct answer.
- **Mail moved into its own part of the table (2026-10-17).** Four rows in five were mail, and most
  searches are not about mail, so every search paid to walk an index mostly made of it. The table is
  now partitioned by source, a plain search reads only the non-mail part, and the vector index stores
  half-precision vectors (an expression index over `halfvec(1024)`; scores are still computed at full
  precision). Rebuilt beside the old table and swapped in once the row and vector counts matched
  (2,354,748 rows). Same answers on all three evaluation sets; the prior-art set fell from 8.9 s to
  2.6 s on average and the main set from 4.9 s to 3.4 s. Building the vector index inside the
  container needs `max_parallel_maintenance_workers = 0`, because Docker's default 64 MB of shared
  memory is too small for a parallel build.
