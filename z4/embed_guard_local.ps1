# embed_guard_local.ps1 - lifecycle manager for the embedding server on a borrowed workstation.
#
# Runs llama-server (Qwen3-Embedding-4B-Q4_K_M, ~3-4 GB VRAM) serving an OpenAI-compatible
# /v1/embeddings endpoint, and CEDES the GPU by KILLING the server, which frees its VRAM completely
# (a paused server would still hold it). The client streams the backlog and retries while the server is
# down; the backlog is idempotent, so a cede loses at most the batch in flight.
#
# Cede (kill the server) when:
#   (a) E:\z4-coord\gpu-preempt.flag is present and fresh: a higher-priority job wants the GPU;
#   (b) owner_busy.ps1 says the owner is using the machine: console unlocked, a remote-desktop peer
#       connected, or (locked) a CAD program doing sustained work. Fails closed.
# Start again only after the owner has been away -ResumeQuietMin minutes (stop at once, resume slowly).
#
# History: process presence (an open Revit blocked the backlog for 20 minutes, 2026-08-25), then CAD CPU
# time alone at 0.15 CPU-s/s (ceded 72 times one night against an idle Revit's 0.05-0.12), then this
# (2026-10-26). quser console-idle and per-process GPU memory are both unusable on this machine.
param(
  [string]$Server     = "E:\llama-embed\llama-server.exe",
  [string]$Model      = "E:\llama-embed\Qwen3-Embedding-4B-Q4_K_M.gguf",
  [int]$Port          = 8081,
  [string]$Flag       = "E:\z4-coord\gpu-preempt.flag",
  [int]$FlagStaleMin  = 15,
  [double]$PollSec    = 2.0,
  [int]$MaxMin        = 1440,
  [int]$ResumeQuietMin = 5,     # after an owner cede, restart only once the owner has been away this long
  [switch]$DryRun
)
$log = "E:\llama-embed\embed_guard.log"
function Now(){ [DateTime]::Now.ToString('o') }
function Log($m){ "$(Now) $m" | Add-Content $log }
function OwnerBusy(){
  # Is the owner using the machine, at the desk or remotely? owner_busy.ps1 (2026-10-26): console unlocked,
  # a remote-desktop peer connected, or (when locked) a CAD program doing sustained work. Replaced the
  # CAD-only CPU check, whose 0.15 threshold sat just above an idle Revit's 0.05-0.12 and ceded 72 times one
  # night with nobody there. Fails CLOSED: any error cedes. Sets $script:OwnerReason for the log.
  try {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File 'E:\llama-embed\owner_busy.ps1'
    $code = $LASTEXITCODE
    try { $script:OwnerReason = ($out | ConvertFrom-Json).reason } catch { $script:OwnerReason = 'owner-busy' }
    return ($code -ne 1)
  } catch { $script:OwnerReason = 'owner check failed'; return $true }
}
function FlagFresh(){
  if(-not (Test-Path $Flag)){ return $false }
  try { return ((New-TimeSpan -Start (Get-Item $Flag).LastWriteTime -End (Get-Date)).TotalMinutes -le $FlagStaleMin) } catch { return $true }
}
function ServerProc(){
  Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -EA SilentlyContinue |
    Where-Object { $_.CommandLine -match 'llama-embed' }
}
function StartServer(){
  if(ServerProc){ return }
  Log "START llama-server :$Port"
  if($DryRun){ Log "DRYRUN would-start"; return }
  # A4000 throughput config (vs Darwin's GTX-1650 batch-of-2/single-stream): 4 parallel
  # slots + room for batched requests. Throughput only: same GGUF and pooling, so the same vector space.
  Start-Process -FilePath $Server -WindowStyle Hidden -ArgumentList @(
    "--model",$Model,"--embedding","--host","0.0.0.0","--port","$Port",
    "-ngl","99","--parallel","4","--ctx-size","8192","--batch-size","4096","--ubatch-size","4096",
    "--threads","4","--threads-batch","8","--flash-attn","on","--mlock","--no-webui"
  ) -RedirectStandardOutput "E:\llama-embed\llama-server.out" -RedirectStandardError "E:\llama-embed\llama-server.err"
}
function StopServer($why){
  $p = ServerProc
  if(-not $p){ return }
  Log "CEDE/STOP ($why)"
  if($DryRun){ Log "DRYRUN would-kill"; return }
  $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
}
$script:LastOwnerBusy = $null
$script:OwnerReason = ''
Log "guard start (dryrun=$DryRun, resume-quiet=${ResumeQuietMin}min)"
$end = (Get-Date).AddMinutes($MaxMin)
try {
  while((Get-Date) -lt $end){
    $cede = $null
    if(FlagFresh){ $cede = "preempt-flag" }              # a higher-priority Z4 job wants the GPU
    elseif(OwnerBusy){ $cede = "owner: $script:OwnerReason"; $script:LastOwnerBusy = Get-Date }
    if($cede){ StopServer $cede; Start-Sleep -Seconds $PollSec; continue }
    # Stop at once, start again only after a quiet spell (HTCondor's shape): no restart churn while he
    # comes and goes, and no loaded model waiting when he sits back down.
    if($script:LastOwnerBusy -and ((Get-Date) - $script:LastOwnerBusy).TotalMinutes -lt $ResumeQuietMin){
      Start-Sleep -Seconds $PollSec; continue
    }
    StartServer
    Start-Sleep -Seconds $PollSec
  }
} finally {
  StopServer "guard-exit"   # never orphan VRAM if the guard ends/dies gracefully
  Log "guard exit"
}


