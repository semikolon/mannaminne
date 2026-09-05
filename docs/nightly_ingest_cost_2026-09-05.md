# The nightly ingest costs the machine every morning — measured 2026-09-05

**Why this file exists.** Fredrik felt the Mac Mini lag (pointer refresh rate, general sluggishness) and said the obvious thing: *"Jag har ALLTID Claude och Beeper uppe. Konstant. Inget som borde få datorn att lagga såhär."* He was right, and the cause is this project's scheduled ingest. Then: *"Om pg insert osv tar såhär lång tid och dröjer in på förmiddagen så måste vi tänka om hur mannaminne ingesten funkar."* This is the measurement behind that.

## What is measured

The ingest is not slow by accident. It **re-chunks and re-upserts nearly the whole corpus every night**, and has done so since it was scheduled on 2026-06-28 (74 runs in `~/.local/share/mannaminne/ingest.log`).

| Run | Start | Complete | Duration |
|---|---|---|---|
| 2026-08-31 | 05:00:05 | 10:17:31 | 5 h 17 m |
| 2026-09-01 | 05:00:05 | 11:17:30 | 6 h 17 m |
| 2026-09-02 | 05:00:03 | 10:27:33 | 5 h 27 m |
| 2026-09-03 | 05:00:02 | 11:13:54 | 6 h 14 m |
| 2026-09-04 | 05:00:05 | 11:33:24 | 6 h 33 m |
| 2026-09-05 | 05:00:02 | still running at 09:10 | 4 h 10 m so far |

**So the machine is under a heavy background job from 05:00 until roughly 11:00 every day** — the whole first half of Fredrik's working morning.

Chunks re-upserted in one run (today's log, one line per kind):

| Kind | Chunks re-upserted |
|---|---|
| screenshot | 132 273 |
| doc | 97 102 |
| code | 28 809 |
| session | 23 108 |
| git_commit | 10 674 |
| email | 2 155 |
| fyr | 795 |
| things3 | skipped (unchanged) |
| email/mbox | skipped (unchanged) |
| **total** | **≈ 295 000** |

The net gain is about **1 500 new chunks per night** (`ingest done:` totals 293 276 → 294 741 → 296 110 → 294 921 across four runs). The table holds **2 240 113** chunks, of which **1 008 241** still lack embeddings (the paused Z4 backfill, `z4/README.md`).

## The mechanism, and it is one line of design

`_skip_if_unchanged(kind, files)` exists and works — it is why `things3` and `email/mbox` are skipped. But it is **per KIND, not per FILE**: if any file in a kind changed, the whole kind is re-chunked and re-upserted. Fredrik writes documents every day, Claude Code writes transcripts every day, screenshots arrive every day, commits land every day. So every large kind changes daily, and every large kind is redone in full, nightly.

## What that costs the machine

Measured at 09:05 while the run was four hours in, on a 17 GB machine:

| Signal | Value |
|---|---|
| Free memory | 0.07 GB |
| Decompressions | 1 215 / s |
| Pageins | 80 / s |
| Swap used | 1.35 GB of 2 GB |
| Load average | 81–108 |
| WindowServer CPU | 85–93 % |
| Ingest process footprint | 3.65 GB (Activity Monitor), 863 MB RSS |
| Data volume free | 15 GB of 228 GB (92 % full) |

The ingest itself uses little CPU (2 m 57 s of CPU over 4 h — it is database-bound, blocked on `INSERT INTO chunks` against `darwin.home:5440`, which Postgres confirms as an active connection). **Its cost is memory and I/O, not CPU.** With free memory at 70 MB the compressor runs continuously, and WindowServer — compositing a 4K panel at 144 Hz for two Electron renderers — is the most visible victim. That last link is inferred from the numbers rather than proven; the discriminating test is to watch WindowServer after the ingest finishes around 11:00.

Darwin is not the bottleneck: load 4.5, 12 GB available, containers up 3 days.

## What to change, cheapest first

1. **Yield to the user (minutes, no code).** The LaunchAgent `com.fredrikbranstrom.mannaminne-ingest.plist` sets only `StartCalendarInterval`. Adding `ProcessType: Background`, `LowPriorityIO: true` and `Nice: 10` makes macOS deprioritise its CPU and I/O against the foreground. This does not shorten the run; it should remove the felt lag.
2. **Per-file skip instead of per-kind (the real fix).** Keep a per-source `(path, mtime, size)` or content-hash record and re-chunk only what changed. The daily work would fall from ~295 000 chunks to the ~1 500 that are actually new, and the run from six hours to minutes. Everything else on this list becomes unnecessary if this lands.
3. **Batch the writes.** Per-row `INSERT` over the network is what makes it database-bound; `COPY` or multi-row batches would cut the wall clock even before the skip fix.
4. **Bound the memory.** A 3.65 GB footprint on a 17 GB machine says the run accumulates rather than streams; chunk, write and release.
5. **Move the deadline, not just the start.** If a full re-ingest is ever needed, it should stop at a wall-clock deadline (say 08:00) and resume the next night rather than run into the working day.

## Separate but related risks noticed in the same pass

- **The data volume is 92 % full (15 GB free)** while the machine swaps. The global rule about disk-pressured hosts and memory-heavy processes exists because of a kernel panic in exactly this shape; the ingest is a >2 GB-resident process running against a nearly full disk every night.
- **Half the corpus has no embeddings** (1 008 241 of 2 240 113 chunks). Semantic search is therefore partial, which the Z4 runbook already records as paused on the acoustic problem at Fredrik's father's house.
