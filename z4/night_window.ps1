# Night carve-out for the embed guard.
#
# The guard cedes on Revit PRESENCE, not activity, so an idle Revit left open
# overnight blocks the backlog indefinitely (measured 2026-08-25: guard ceded
# continuously for 20 min with Revit merely open). Between NightStart and
# NightEnd that presence is ignored: Mats is asleep, an open window is not work.
#
# The gpu-preempt.flag cede is NEVER overridden — that is the explicit
# higher-priority-job signal and must win at any hour.
param(
  [int]$NightStart = 1,
  [int]$NightEnd   = 7
)
$h = (Get-Date).Hour
if($h -ge $NightStart -and $h -lt $NightEnd){ exit 0 }   # night: allow
exit 1                                                    # day: defer to normal cede
