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
utredning: `~/dotfiles/docs/z4_access_and_darwin_outage_2026-08-25.md`.

Tunneln på Macen: `~/.config/wireguard-mac/mzvpn.conf`, delad tunnel, beständig via
`/Library/LaunchDaemons/com.fredrikbranstrom.wireguard-mzvpn.plist`. **⚠ Delad nyckel med pappas
laptop till 2026-08-26** — en aktiv anslutning per nyckel, så en av oss faller tyst bort om båda
kopplar upp. Han reser fredag; dedikerad klient var **utlovad den 26:e och har inte kommit** (kollat
2026-08-26 22:00, `mzvpn.conf` orörd sedan den 25:e). Tas upp på samtalet med honom.

**⏸ Backfillen är pausad sedan 2026-08-26 — högtalarbrus hos pappa.** När A4000:an
belastas hörs brus ur hans högtalare **i hans kontor** — alltså där han arbetar, inte i ett
sällskapsrum, vilket är varför gränsen är hans arbetsro och inte bara trivsel. Uppmätt tyst
vid 22 W, brus vid 129 W (en körning ligger på 124-132 W, alltså mitt i registret), över tre
av- och påslag. Det är en jordslinga på högtalarsidan, inte något digitalt och inget vi
orsakar utöver att vara den första ihållande GPU-lasten som blottar den. Full utredning
med vad som uteslutits: `~/dotfiles/TODO.md`, sök "högtalarbrus". **Stäm av med honom först.**

**⚠ `kill -CONT <pid>` gäller inte längre (2026-09-05).** Macen startades om 13:16 den dagen
för macOS 26.6.2, så den avstannade klienten finns inte kvar — det går ingen process att
återuppta. Att återuppta betyder numera att starta klienten på nytt enligt "Run / check / stop"
nedan. Kön var 1 008 246 av 2 240 118 chunks vid omstarten, alltså **cirka sju timmar vid
uppmätta 40 chunks/s**: en nattkörning, inte något som hinner bli klart under ett arbetspass.

**Vakten hör inte högtalarna.** `cad_working.ps1` mäter CAD-processernas CPU-tid och säger
alltså bara om pappa *arbetar*, inte om han är *hemma*. Mätt 2026-09-05 14:10 var Revit öppet
men vilande, 0,045 CPU-sekunder per väggsekund mot tröskeln 0,15 — vakten hade startat servern.
Det är precis det läge den akustiska pausen finns för: grönt ljus från vakten är inte samtycke
från honom.

### Att stoppa en körning — `z4-embed-stop`

`~/.local/bin/z4-embed-stop` (nit-spårat) gör hela stoppsekvensen: dödar Mac-klienten, kör
`schtasks /end` **och** `kill_embed.ps1` på Z4:an, verifierar att ingen `llama-server` ligger
kvar och pingar `fleet`-topicen. Båda Windows-stegen behövs — `/end` hoppar över vaktens
städning och lämnar annars servern föräldralös med VRAM taget.

**En körning med deadline får två oberoende lager**, eftersom ett missat stopp hörs hemma hos
honom: (1) en engångs-LaunchAgent med `StartCalendarInterval` som kör stoppskriptet på slaget
och sedan avinstallerar sig själv — den överlever en omstart av Macen, vilket en sovande
skalprocess inte gör; (2) `MANNAMINNE_EMBED_MAX_SECONDS` på klienten, som stannar sig själv
oavsett vad som händer med agenten. Klienten skriver var 500:e chunk, så ett stopp när som
helst kostar högst den påbörjade halvminuten och nästa körning fortsätter där den slutade.

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
- **Throughput ~10 chunks/s på olik text — AVGJORT 2026-09-05, och 40/sec var aldrig sant.**
  Både den siffran och forskningsdokumentets 112/sek kom från riktmärken med *upprepad
  identisk* text, alltså llama.cpp:s prefix-cache och inte inbäddning: 16 identiska strängar
  ger 46,5/s, 16 OLIKA riktiga chunks ger 9,0/s. **Ett riktmärke för en inbäddningstjänst
  måste mata olik text.** Takten är platt över batchstorlek (16/32/64 → 9,0/10,0/9,6) och
  oberoende av klientens parallellism (2 arbetare/batch 4 → 4/32 ändrar ingenting, mätt både
  i juni och i september), så kortet är compute-bundet på prefill. Juni-dokumentets otestade
  serverrekommendation (ubatch 8192, ctx 16384) deployades samma dag och **gjorde det sämre**:
  7,0-7,8/s, VRAM 9,4 → 13,0 GiB, återställd. **Vid 10/s är en miljonchunkskö ~28 timmar, inte
  en kväll** — planera fönstren därefter. Enda kvarstående hypotes är Q4-dekvantiseringen;
  otestad, och ett byte till F16 bryter vektorrymden mot de 1,23 miljoner redan inbäddade.
  Full mätning: `~/dotfiles/docs/z4_gpu_idle_parsec_throughput_research_2026_06_12.md` § AVGJORT.
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

## Getting Darwin to reach the Z4 directly — corrected 2026-09-05, nothing to install

**An earlier draft of this section, written the same day, said to install WireGuard on the Z4 and
dial Darwin. That was wrong and it was written before reading
`~/Projects/swhisper-work/docs/z4_transcription_offload_2026_06_27.md`, which had already settled
the topology.** Keeping the correction visible because the wrong version is the intuitive one and
someone will re-derive it.

**The tunnel already exists and is live.** Measured 2026-09-05 on Darwin:

| fact | measurement |
|---|---|
| Darwin `wg0` | `10.0.0.1/24`, port 51820, NAT masquerade so peers reach the Sarpetorp LAN |
| peer `10.0.0.6`, commented "Z4 (Mats RTX A4000) — added 2026-06-29" | **handshake 20 s ago**, endpoint `89.233.228.17`, 798 KiB in / 219 KiB out |
| WireGuard on the Z4 itself | **absent** — no `wg.exe`, no service, no `C:\Program Files\WireGuard` |
| `ping 10.0.0.6` from Darwin | silent |
| Darwin → `192.168.0.233` (the Z4) and `192.168.0.1` (dad's router) | silent |
| Darwin's routes on `wg0` | `10.0.0.0/24` only |

Those two rows together settle what holds `10.0.0.6`: **dad's own router, not the Z4** — which is
what the 2026-08-25 correction in the swhisper-work doc concluded from the other direction. So dad
never needs WireGuard on the Z4; his router is already a peer of Darwin's mesh and has been since
June.

**What is actually missing is routing, on both ends, and no software anywhere:**

1. ~~**On Darwin**, widen the peer's `AllowedIPs`.~~ **Done 2026-09-05** — and it turned out to be
   a re-application, not a discovery: commit `47b12d47` had already put
   `AllowedIPs = 10.0.0.6/32, 192.168.0.0/24` in the router role template, and the live machine had
   drifted away from it. **The overlay was ahead of the machine, which is the opposite of the usual
   assumption.** Verified after the change: the allowed-ips and the route are in place on Darwin,
   and nothing on `192.168.0.0/24` answers on four ports, which localises what remains to step 2.
   Original wording: the peer's `AllowedIPs = 10.0.0.6/32` admitted only the router itself. To reach
   machines behind it, it needs `10.0.0.6/32, 192.168.0.0/24`; wg-quick then installs the route.
   One line in `/etc/wireguard/wg0.conf`, reversible, and it belongs in the nit-tracked Darwin
   overlay like the rest.
2. **On dad's router: the WireGuard-client "Inbound Firewall" toggle must be set to Allow.**
   Corrected the same day — this was written as an unknown, and it was not: the setting is named in
   `dotfiles/system/roles/router/etc/wireguard/wg0.conf.template`, in a comment added with the Z4
   peer on 2026-06-29. Reading the overlay before designing would have skipped the detour.
   Only Mats can set it, and it is a toggle rather than an install.

The silent `ping 10.0.0.6` is not alarming on its own: routers commonly ignore ICMP on a tunnel
interface. Prefer a protocol that answers when testing, per the method note in the swhisper doc — an
early `nc -zvu` there reported success against a dead path, because UDP reports success on silence.

Until routing is in place a backfill runs from the Mac, which is the only machine on both tunnels
(`10.0.0.4` on Darwin's mesh, `10.6.0.2` on dad's). That costs little day to day: the client commits
progress per batch and resumes, so a Mac restart loses at most the batch in flight.
