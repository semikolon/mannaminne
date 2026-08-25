# Is Mats ACTUALLY working in a CAD app right now, as opposed to having one open?
#
# Exit 0 = working (cede)   Exit 1 = open-but-idle, or absent (safe to run)
#
# WHY THIS EXISTS
# The guard cedes on process PRESENCE. Measured 2026-08-25: it ceded continuously
# for 20 minutes with Revit merely open and doing nothing, which blocks the
# backlog indefinitely. Presence answers the wrong question.
#
# WHY CPU-TIME AND NOT GPU UTILISATION
# Aggregate utilization.gpu does work (10 samples, Revit open, 0% throughout) and
# the June note calling detection impossible only ever tested PER-PROCESS metrics,
# which really are [N/A] here. But utilisation has a fatal flaw for this job: our
# own llama-server drives it to ~100%, so once we start, foreign load is
# unmeasurable and mid-run protection disappears.
#
# Per-process CPU time has neither problem. It attributes directly to Revit, and
# it survives GPU contention — so the same check guards both the decision to start
# AND the whole run. Measured idle: 0.05 CPU-seconds over 8s wall (~0.6% of one core).
param(
  [double]$WallSec   = 6,
  [double]$BusyRatio = 0.15,  # CPU-seconds per wall-second, summed across CAD procs
  [string[]]$Names   = @('Revit','acad','StatConStructure','Robot','AutoCAD')
)
function CadProcs(){ Get-Process -Name $Names -EA SilentlyContinue }

$before = @{}
foreach($p in (CadProcs)){ $before[$p.Id] = $p.TotalProcessorTime.TotalSeconds }
if($before.Count -eq 0){ exit 1 }              # no CAD at all -> safe

Start-Sleep -Seconds $WallSec

$delta = 0.0
foreach($p in (CadProcs)){
  if($before.ContainsKey($p.Id)){
    $delta += ($p.TotalProcessorTime.TotalSeconds - $before[$p.Id])
  } else {
    exit 0                                     # a CAD process appeared mid-sample -> he is starting work
  }
}
$ratio = $delta / $WallSec
if($ratio -ge $BusyRatio){ exit 0 } else { exit 1 }
