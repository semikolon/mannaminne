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
| `embed_guard_local.ps1` | The lifecycle manager: starts, cedes and restarts the server. |
| `cad_working.ps1` | Says whether a CAD program is actually working, by its own CPU time, as opposed to merely open. Fails closed. |
| `foreign_gpu_load.ps1` | Says whether something other than the embedder is loading the GPU. |
| `quser_idle.ps1`, `idle_research.ps1` | Console-idle signals. Measured unreliable on the machine this was written for; kept for reference. |
| `kill_embed.ps1` | Stops the server by its exact path. Needed after ending the task, which skips the guard's own cleanup. |
| `smoke_embed.ps1` | Starts the server, embeds two strings, stops it. |

What was learned building it:

- **Cede on activity, not on presence.** An open program is not work. Measuring
  the program's own CPU time over a few seconds tells the two apart and keeps
  working while the embedder itself has the GPU at 100 %.
- **A benchmark for an embedding server must feed different texts.** Repeating
  one string measures the prefix cache: 46 strings a second against 9 for real
  text on the same card.
- **Ending the scheduled task orphans the server.** Always run `kill_embed.ps1`
  afterwards.

The runbook for the actual machine (how to reach it, when it may run, what has
gone wrong) describes another person's workstation and network, so it is kept
privately: `~/dotfiles/docs/z4_embedder_runbook.md`.
