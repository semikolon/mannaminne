# Borrowed-GPU embedder

Scripts for running the embedding server on a Windows workstation that belongs
to someone else, so that a large backfill can use its GPU without the owner
ever noticing. The server is llama.cpp's `llama-server` with the same GGUF the
home embedder serves, so both write into one vector space.

The rule the scripts enforce: **the owner's work always wins.** The guard
starts the server only when the machine is not being worked on and kills it
(freeing the GPU memory, not pausing) the moment that changes.

| script | what it does |
|---|---|
| `run_embed.bat` | Entry point for the scheduled task. Starts the guard. |
| `embed_guard_local.ps1` | The lifecycle manager: starts the server, kills it when the owner is busy or a preempt flag is up, and restarts it only after a quiet spell. |
| `owner_busy.ps1` | The one check every job on the machine yields to: the console is unlocked, a remote-desktop peer is connected, or (while locked) a CAD program is doing sustained work. Fails closed. |
| `test_owner_busy.ps1` | Branch test for it, with simulated lock states and remote-desktop logs. |
| `cad_working.ps1` | The earlier check (CAD CPU time alone). Superseded by `owner_busy.ps1`; kept for reference. |
| `foreign_gpu_load.ps1` | Says whether something other than the embedder is loading the GPU. |
| `quser_idle.ps1`, `idle_research.ps1` | Console-idle signals. Measured unreliable on the machine this was written for; kept for reference. |
| `kill_embed.ps1` | Stops the server by its exact path. Needed after ending the task, which skips the guard's own cleanup. |
| `smoke_embed.ps1` | Starts the server, embeds two strings, stops it. |

What was learned building it:

- **Cede on activity, not on presence.** An open program is not work. Measuring
  the program's own CPU time over a few seconds tells the two apart and keeps
  working while the embedder itself has the GPU at 100 %.
- **But CAD CPU time alone has a thin margin and a blind spot.** Two idle Revit
  windows used 0.05-0.12 CPU-seconds per second against a 0.15 threshold, which
  ceded 72 times in one night with nobody there; and it cannot see the owner
  reading mail at the desk or working remotely. The session's lock state (whether
  `LogonUI.exe` runs in the console session) and the remote-desktop host's own
  connect/disconnect log cover both, so the CAD
  threshold only has to catch work left running while the machine is locked.
- **The documented lock flag read wrong here.** `WTSQuerySessionInformation`
  with `WTSSessionInfoEx` returned *locked* for a whole morning of CAD work at the
  console; whether `LogonUI.exe` runs is the reading that matched what happened.
- **Stop at once, start slowly.** After a cede the guard waits for a quiet spell
  before restarting, the shape HTCondor's desktop policy uses.
- **GPU load barely shows CAD work.** In about 2,100 minutes of journalled CAD
  work, aggregate GPU use was above 1 % in only 30 % of them.
- **A benchmark for an embedding server must feed different texts.** Repeating
  one string measures the prefix cache: 46 strings a second against 9 for real
  text on the same card.
- **Ending the scheduled task orphans the server.** Always run `kill_embed.ps1`
  afterwards.

The runbook for the actual machine (how to reach it, when it may run, what has
gone wrong) describes another person's workstation and network, so it is kept
privately: `~/dotfiles/docs/z4_embedder_runbook.md`.
