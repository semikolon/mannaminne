# Z4 embedder — mannaminne backlog on Mats's RTX A4000

Semantic-embedding backend for mannaminne (and the same-space fallback story for Graphiti).
Runs Qwen3-Embedding-4B on the Z4's A4000 instead of Darwin's contended GTX-1650 for large
batch backfills. **Status (2026-06-14): backlog complete — 934,764/934,764 chunks embedded, HNSW built, Z4 server/guard/client stopped.** Darwin remains the live query-embedding fallback/standing endpoint.

**⚡ NEW BACKLOG — this runbook is live again. Backfill COMPLETE 2026-08-14.** All 123 338 messages
fetched (26 000 in the first run, 97 413 in the second after a bug fix, **0 failures**). The twelve-year
hole is closed: the email corpus now runs continuously **2010-05-16 → 2026-08-13**, 1 797 794 chunks,
every year populated. Full-text search over all of it is live immediately.

**The real backlog is 1 129 154 chunks pending embed — 2.5× my pre-run estimate of ~450 000.** At
Darwin's measured 0.55 chunks/s that is **~570 hours**, which is what moves the A4000 from
nice-to-have to necessary.
Measured drain rate on Darwin: **~0.55 chunks/s** (1 000 chunks in ~31 min — the GTX 1650 running a
4B model single-slot at `--ctx-size 512` with long chunks; close to that card's floor, not a fault),
so Darwin alone needs **~200 hours**. The A4000 is the intended instrument.

**Status 2026-08-16:** embed queue down to **1 066 957 of 2 205 559** chunks (from 1 129 154), so
Darwin is draining at roughly the measured 0.55/s and will not finish this decade. Z4 still
unreachable — the Windows firewall step below has not been run. Keyword search covers the whole
corpus meanwhile; only semantic search lags.

**Reach status 2026-08-13 (verified):** the Z4 IS a live WireGuard peer — `10.0.0.6`, handshake
seconds old, traffic flowing. Two blockers sit above it before `ssh z4` works again: the Z4's Windows
Firewall does not admit the tunnel subnet, and Darwin's `FORWARD` chain drops NEW peer-to-peer
traffic. Both diagnosed with exact fixes in
`~/Projects/swhisper-work/docs/z4_transcription_offload_2026_06_27.md` § Access-path STATUS
2026-08-13. Clear those, repoint the `z4` ssh host to `10.0.0.6`, then follow "Run / check / stop"
below unchanged.

## ⚡ ÅTKOMST LÖST + nattfönster (2026-08-25)

**`ssh z4` fungerar igen.** Vägen in är **pappas egen router-VPN**, inte den utåtringande tunneln som
den här körboken förutsatte. CGNAT-diagnosen som blockerat i två månader var fel — TCP 443 till hans
adress svarar, alltså är linjen nåbar; det är port 2224 ensam som inte är vidarebefordrad. Full
utredning: `~/dotfiles/docs/z4_atkomst_och_darwin_avbrott_2026-08-25.md`.

Tunneln på Macen: `~/.config/wireguard-mac/mzvpn.conf`, delad tunnel, beständig via
`/Library/LaunchDaemons/com.fredrikbranstrom.wireguard-mzvpn.plist`. **⚠ Delad nyckel med pappas
laptop till 2026-08-26** — en aktiv anslutning per nyckel, så en av oss faller tyst bort om båda
kopplar upp. Han reser fredag; dedikerad klient var **utlovad den 26:e och har inte kommit** (kollat
2026-08-26 22:00, `mzvpn.conf` orörd sedan den 25:e). Tas upp på samtalet med honom.

**⏸ Backfillen är pausad sedan 2026-08-26 — högtalarbrus hos pappa.** När A4000:an
belastas hörs brus ur hans högtalare: uppmätt tyst vid 22 W, brus vid 129 W, över tre
av- och påslag. Det är en jordslinga på högtalarsidan, inte något digitalt och inget vi
orsakar utöver att vara den första ihållande GPU-lasten som blottar den. Full utredning
med vad som uteslutits: `~/dotfiles/TODO.md`, sök "högtalarbrus". Återuppta med
`kill -CONT <pid>` på Mac-klienten, men stäm av med honom först.

**Ceden gäller nu AKTIVITET, inte närvaro** (ersatte nattfönstret samma dag, som var en sämre
lösning: det skyddade bara utanför 01–07 och såg inte en rendering klockan tre). `cad_working.ps1`
mäter CAD-processernas egen CPU-tid över ett fönster på sex sekunder och cedar när summan överstiger
0,15 CPU-sekunder per väggsekund. Verifierat i båda riktningarna: ett öppet men vilande Revit gav
0,05 CPU-sekunder på åtta (alltså vila, servern startade), och mot en syntetiskt belastad process
utlöste den korrekt. Fail-closed: fel i mätningen cedar.

**Varför CPU-tid och inte GPU-belastning**, trots att aggregerad `utilization.gpu` bevisligen
fungerar: vår egen server driver den till ~100 %, så främmande last blir omätbar så fort vi kört
igång och skyddet mitt i körningen försvinner. Per-process CPU-tid överlever kontention och vaktar
därför hela körningen, inte bara startbeslutet.

**Historik (borttaget):** nattfönster 01–07.

**`gpu-preempt.flag` åsidosätts aldrig** — den är den uttryckliga
högre-prioritet-signalen och vinner när som helst på dygnet.

Kvarstående risk, låg men verklig: en lång rendering eller export som lämnas igång över natten är
inte ett vilande Revit. Pappa nämner sådant i regel.

**Ceden är verifierad i skarpt läge**, inte bara i teorin: den fångade en levande Revit-session och
vägrade starta servern. Originalvakten säkerhetskopierad som `embed_guard_local.ps1.bak-20260825`.

**Bevakare på Macen:** `~/.local/bin/z4-embed-watch` (LaunchAgent, var tionde minut) pingar ntfy när
servern faktiskt startar och när den stannar, med kvarvarande kö. Enbart läsande — den startar,
stoppar eller åsidosätter aldrig vakten.

## Architecture
- **Model** — `Qwen3-Embedding-4B-Q4_K_M.gguf`, **byte-identical to Darwin's** (sha256
  `2b0cf8…`). Same GGUF + same llama.cpp pooling ⇒ **same vector space**, so chunks already
  embedded by Darwin stay valid and Darwin remains a true semantic fallback. This is why we
  mirror Darwin's Q4 rather than serve FP16 via Infinity: FP16 is a *different* numeric space
  (dimensionality stays 1024 via the client's MRL truncation; precision is what differs) and
  would force a full re-embed + drop Darwin as a fallback. Throughput gain of FP16 is
  non-load-bearing for a one-time backlog.
- **Server** — llama.cpp `llama-server.exe` (b9610 win-cuda-12.4) at `E:\llama-embed\`,
  OpenAI-compat `/v1/embeddings` on `0.0.0.0:8081`, `--parallel 8 --ctx-size 8192
  --batch-size 2048` (A4000 tensor cores; Darwin's single-stream/batch-2 config throttled it
  to ~3/sec — see "throughput" below).
- **Reach** — the Z4's :8081 is NOT WAN-exposed (only ssh is); the Mac client reaches it via
  an SSH tunnel `ssh -L 8081:127.0.0.1:8081 z4`, kept alive by the launchd agent
  `com.fredrikbranstrom.z4-embed-tunnel` (`~/Library/LaunchAgents/`, KeepAlive + ServerAliveInterval).
  Built 2026-08-25 after an ad-hoc tunnel died and stalled the backfill at 15 952 chunks with
  "connection refused" while both ends were healthy — only the pipe between them had gone.
- **Client** — `mannaminne embed` on the Mac (psycopg → Darwin Postgres `:5440`), batch-of-8,
  `MANNAMINNE_EMBED_URL=http://127.0.0.1:8081/v1/embeddings`, run under `caffeinate`, backs
  off 30s when the server is down/ceded. Pipeline proven (commits land).
- **Guard** (`embed_guard_local.ps1`, scheduled task `z4-embed-server`) — the lifecycle
  manager: starts / cedes / restarts llama-server. **Cede = KILL the server** (fully frees
  VRAM; a paused-but-loaded server would still hold ~4 GB and block higher-priority Z4 jobs).

## Mats-safety (the load-bearing constraint)
The Z4 is Mats's daily workstation (he works locally, and occasionally remotely via Parsec).
Production runs **always-on (CarveOut=1)** with a **CAD-presence cede** (`run_embed.bat` passes
`-CarveOut 1`):
- **No idle gate** — both auto-idle signals are broken on this box (see below), so the guard runs
  continuously and protects Mats by ceding, not by gating.
- **Cede** (~1.5s, kills the server) when: **Revit/AutoCAD is process-present** in
  `nvidia-smi --query-compute-apps` (Mats doing local CAD — works despite `[N/A]` memory), OR
  `E:\z4-coord\gpu-preempt.flag` appears (a higher-priority Z4 job preempts).
- **Active-Parsec** (rare) is NOT auto-detected (`parsecd.exe` is GPU-present even when nobody's
  connected = false positive). Fredrik flags it manually; the 45-min health loop also watches.
- Reassurance: a GPU at 100% does not lock up the machine (CPU/RAM/UI stay fine); only GPU apps
  (CAD viewport, or an active Parsec stream) feel it — and CAD triggers the cede.

**⚡ RÄTTELSE 2026-08-25 — aggregerad GPU-belastning FUNGERAR, och det testades aldrig i juni.**
Juni-utredningen prövade **per-process**-mätvärden (minne, `pmon`) och båda är verkligen `[N/A]` på
den här drivrutinen. Slutsatsen blev "detektion är omöjlig" och letandet stannade där. Men den
**aggregerade** räknaren `nvidia-smi --query-gpu=utilization.gpu` fungerar utmärkt: mätt 2026-08-25
med Revit ÖPPET gav tio prov på tio sekunder **0 % rakt igenom**. Öppet och arbetande går alltså att
skilja åt — det var aldrig omöjligt, bara omätt på rätt räknare.

Varför det är bättre än nattfönstret: det skyddar Mats dygnet runt i stället för bara utanför 01–07,
och det svarar på vad han faktiskt gör i stället för vad han råkat lämna öppet. En rendering klockan
tre på natten syns; för nattfönstret är den osynlig.

Haken, och den är verklig: vår egen embed-server driver belastningen till 100 %, så en naiv koll
cedar mot sig själv. Regeln måste vara *"hög belastning som INTE är vår egen"*.

**🚨 Why no idle gate — both PER-PROCESS auto-signals are broken on this A4000 (2026-06-12):**
- **Per-process GPU memory reads `[N/A]`** (`nvidia-smi --query-compute-apps=...,used_memory` →
  `[N/A]`), so any memory-jump cede is BLIND. (Same flaw hits brf-auto's `gpu_guard_local.ps1` —
  flagged in the council doc.)
- **`quser` console-idle is unreliable** — it reads "active" for hours after Mats physically
  leaves, so an idle-≥20-min gate would essentially never fire (backlog would stall). (This also
  means brf-auto's `LAUNCH_IDLE_MIN` OCR gate may never trigger here — flagged for them.)
- Therefore **process-presence** (is Revit/AutoCAD running?) is the only working "Mats doing GPU
  work" signal, and the embedder runs always-on + cedes on it.

## Run / check / stop
- **Future batch run**: start the `z4-embed-server` guard task, start the SSH tunnel supervisor,
  then run the Mac client under `caffeinate` with
  `MANNAMINNE_EMBED_URL=http://127.0.0.1:8081/v1/embeddings`. The 2026-06-13 backlog is
  complete; this runbook is for future backfills/re-embeds.
- **Check count** — `cd ~/Projects/mannaminne/py && .venv/bin/python` then `load_conn()` +
  `SELECT count(*), count(embedding) FROM chunks`. (Do NOT shell-source `db.env` for psql —
  the password mangles in the shell; use the tool's own `psycopg` connection.)
- **Restart guard** — `ssh z4 'schtasks /run /tn z4-embed-server'` (starts the local guard; it
  launches/stops the server per the current `embed_guard_local.ps1` policy).
- **Stop cleanly** — `ssh z4 'schtasks /end /tn z4-embed-server'` **then**
  `ssh z4 'powershell -File E:\llama-embed\kill_embed.ps1'` (the `/end` force-kills the guard
  so its cleanup is skipped → `kill_embed` prevents an orphaned server).
- **Deploy script changes** — `scp z4/*.ps1 z4/*.bat z4:E:/llama-embed/` then restart the task.
- **Idle-window fallback** — `-CarveOut 0` is available, but is not production on this box:
  `quser` stayed active for hours after Mats left, so idle-only mode can stall indefinitely.

## Known caveats (fix when touched)
- **Orphan on force-kill** — `schtasks /end` skips the guard's `finally`, orphaning the server
  (holds VRAM, uncede-protected). Always `kill_embed.ps1` after; or add a graceful stop-marker;
  or a periodic orphan-sweep (cf. brf-auto `docker-logs-orphan-sweep`).
- **CAD detection is process-presence only** — `Revit|acad` in `nvidia-smi` is the working local
  CAD signal. It deliberately ignores background `parsecd.exe`; active Parsec still needs
  operator/health-loop attention.
- **Throughput ~40/sec** — overnight-clearable. Tunable higher (more `--parallel`, bigger
  client batches) but not worth it for a one-time backlog.
- **Permanently-failing rows** — a chunk that always errors stays NULL → the client backoff-
  loops on it at the tail. Mark/skip if the backlog stalls near-done.

## Visibility
Committed **LOCALLY only — NOT pushed.** The mannaminne remote (`semikolon/mannaminne`, renamed from `ccsearch` 2026-08-13) is
PUBLIC; these scripts reveal fleet/Mats topology AND the repo hardcodes a FalkorDB dev
password (`discover_fyr`). Do not push without sanitizing + a visibility audit.

## Cross-refs
- Cross-project Z4 strategy + cede coordination + same-space reasoning:
  `~/dotfiles/docs/z4_local_model_strategy_cross_project_2026_06_11.md`
- mannaminne design: `~/dotfiles/docs/personal_archives_semantic_search_2026_06_10.md`
- Reused guards: `~/Projects/brf-auto/lib/z4_trial/guard/`
