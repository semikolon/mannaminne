# Query sets

A query set is a list of questions with what a good answer must contain.
`mannaminne eval` runs each question and reports recall, mean reciprocal rank
and latency against the live index.

**The real sets are not in this repository.** Questions written against one
person's corpus say what is in that corpus, so they are that person's data.
They live in `~/.config/mannaminne/eval/` (or wherever `MANNAMINNE_EVAL_DIR`
points), beside the other personal lists.

```bash
mannaminne eval                              # ~/.config/mannaminne/eval/golden_queries.json, else the example here
mannaminne eval --file golden_queries_sv.json   # a bare name is looked up in the private directory
mannaminne eval --file /some/other/set.json     # a path is used as given
```

`example_queries.json` shows the format with invented questions. It will score
badly against any real corpus; it is there to be copied and rewritten.

Until 2026-10-11 three real sets were tracked here (`golden_queries.json`,
`golden_queries_sv.json`, `golden_prior_art.json`). Older notes that point at
`eval/golden_*.json` mean the files of the same names in the private directory.
