# Is someone ELSE using the GPU right now?
#
# Returns 0 (yes, cede) / 1 (no, safe to run).
#
# Why this exists: the guard cedes on Revit PRESENCE, so an idle Revit left open
# blocks the backlog forever (measured 2026-08-25: 20 min of continuous cede with
# only an open window). Presence is the wrong question; load is the right one.
#
# Why aggregate and not per-process: per-process memory reads [N/A] on this A4000
# and pmon shows dashes, which is what made the June investigation conclude
# detection was impossible. It checked the wrong counter. The AGGREGATE
# utilization.gpu works: ten samples with Revit open measured 0% throughout.
#
# The self-exclusion problem: our own llama-server drives utilisation to ~100%,
# so a naive check cedes against itself. Hence: when our server is running, high
# load is presumed OURS and this returns "safe" — the guard must not kill its own
# work. Foreign load is therefore only detected while we are NOT running, which is
# exactly the moment the decision matters (should we start?). Once running, the
# PRESENCE check still guards mid-run, plus the preempt flag.
param(
  [int]$Samples   = 5,
  [int]$IntervalMs = 700,
  [int]$Threshold = 12   # % — above this, someone is doing real GPU work
)
$ours = @(Get-Process llama-server -EA SilentlyContinue).Count
if($ours -gt 0){ exit 1 }   # our own load; presence-check + flag guard mid-run

$vals = @()
1..$Samples | ForEach-Object {
  try { $vals += [int](nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>$null) } catch {}
  Start-Sleep -Milliseconds $IntervalMs
}
if($vals.Count -eq 0){ exit 0 }   # can't measure -> assume busy, protect Mats

# Median, not mean: a single spike from a window redraw should not cede.
$sorted = $vals | Sort-Object
$median = $sorted[[int]($sorted.Count/2)]
if($median -ge $Threshold){ exit 0 } else { exit 1 }
