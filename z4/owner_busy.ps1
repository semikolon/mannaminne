# Is the workstation's owner using it, at the desk or remotely? The one check every borrowed-GPU job yields to.
#
# Exit 0 = busy (yield)   Exit 1 = free (may run). Prints one JSON line with the reason.
#
# Signals, in order (built 2026-10-26 from a review of the cede history on the machine and of how HTCondor,
# BOINC, Deadline and Salad yield to a desktop's owner):
#   1. The console session is UNLOCKED -> busy. This machine locks five minutes after the last input, so an
#      unlocked session means someone has been there within minutes, whatever program they use (mail, web,
#      CAD). Read with WTSQuerySessionInformation(WTSSessionInfoEx).SessionFlags, which works from session 0
#      (a service or SSH), unlike GetLastInputInfo or quser idle, which both measured wrong here.
#   2. A remote-desktop peer is CONNECTED (the Parsec host log's last "<peer> connected/disconnected" line)
#      -> busy. The owner works remotely now and then; the stream needs the GPU.
#   3. Locked and nobody connected: busy only if a CAD program is doing sustained work (a render or export
#      left running): its own CPU time at or above -LockedRatio in two 6 s windows in a row. Revit left open
#      idles at 0.05-0.12 CPU-seconds per second; the old single-window 0.15 threshold ceded 72 times in one
#      night with nobody there.
#   If the lock state cannot be read, fall back to the old rule (0.15 in one window): cautious, never free by
#   default. Any error -> busy. Nobody logged in at the console -> treated as locked.
#
# -Log appends the reading to a CSV, for calibrating against the CAD program's own journal later.
param(
  [double]$WallSec       = 6,
  [double]$LockedRatio   = 0.5,
  [double]$FallbackRatio = 0.15,
  [string[]]$Names       = @('Revit','acad','StatConStructure','Robot','AutoCAD'),
  [string]$LogCsv        = '',
  [string[]]$ParsecLogs  = @('C:\Users\*\AppData\Roaming\Parsec\log.txt', 'C:\Users\*\AppData\Roaming\Parsec\log.1.txt'),
  [string]$AssumeLock    = ''   # tests only: 'locked' / 'unlocked' / 'unknown' instead of reading the session
)
$ErrorActionPreference = 'Stop'

function Out-Verdict([bool]$busy, [string]$reason, [hashtable]$extra) {
  $o = [ordered]@{ busy = $busy; reason = $reason; at = (Get-Date).ToString('s') }
  foreach ($k in $extra.Keys) { $o[$k] = $extra[$k] }
  ($o | ConvertTo-Json -Compress)
  if ($LogCsv) {
    try {
      if (-not (Test-Path $LogCsv)) { 'at,busy,reason,lock,peer,cad_ratio' | Set-Content $LogCsv }
      $ci = [Globalization.CultureInfo]::InvariantCulture
      [string]::Format($ci, '{0},{1},{2},{3},{4},{5}', $o.at, [int]$busy, $reason, $extra.lock, $extra.peer, $extra.cad_ratio) | Add-Content $LogCsv
    } catch {}
  }
  if ($busy) { exit 0 } else { exit 1 }
}

try {
  Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class OwnerWts {
  [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSQuerySessionInformation(IntPtr h, int sid, int cls, out IntPtr buf, out int bytes);
  [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr p);
  [DllImport("kernel32.dll")] public static extern int WTSGetActiveConsoleSessionId();
  // WTSSessionInfoEx (25) -> WTSINFOEXW { int Level; WTSINFOEX_LEVEL1_W { int SessionId; int SessionState; int SessionFlags; ... } }
  public static int Flags(int sid) {
    IntPtr p; int n;
    if (!WTSQuerySessionInformation(IntPtr.Zero, sid, 25, out p, out n)) return -2;
    try { return Marshal.ReadInt32(p, 12); } finally { WTSFreeMemory(p); }
  }
  public static string User(int sid) {
    IntPtr p; int n;
    if (!WTSQuerySessionInformation(IntPtr.Zero, sid, 5, out p, out n)) return null;
    try { return Marshal.PtrToStringUni(p); } finally { WTSFreeMemory(p); }
  }
}
"@ -ErrorAction SilentlyContinue

  $sid = [OwnerWts]::WTSGetActiveConsoleSessionId()
  $user = if ($sid -ge 0 -and $sid -ne -1) { [OwnerWts]::User($sid) } else { $null }
  $flags = if ($sid -ge 0) { [OwnerWts]::Flags($sid) } else { -2 }
  # SessionFlags: 0 = locked, 1 = unlocked (reversed only on Windows 7 / 2008 R2), -1 = unknown
  $lock = if ($AssumeLock) { $AssumeLock } elseif (-not $user) { 'nobody' } elseif ($flags -eq 0) { 'locked' } elseif ($flags -eq 1) { 'unlocked' } else { 'unknown' }

  # Remote-desktop peer: the last connect/disconnect line in the Parsec host log (and its rotated copy).
  $peer = 'none'
  foreach ($f in @(Get-ChildItem $ParsecLogs -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
    $m = Select-String -LiteralPath $f.FullName -Pattern '\] \S+ (connected|disconnected)\.\s*$' | Select-Object -Last 1
    if ($m) { $peer = if ($m.Matches[0].Groups[1].Value -eq 'connected') { 'connected' } else { 'none' }; break }
  }

  $info = @{ lock = $lock; peer = $peer; cad_ratio = '' }
  if ($lock -eq 'unlocked') { Out-Verdict $true 'console unlocked' $info }
  if ($peer -eq 'connected') { Out-Verdict $true 'remote desktop connected' $info }

  function CadRatio() {
    $before = @{}
    foreach ($p in (Get-Process -Name $Names -ErrorAction SilentlyContinue)) { $before[$p.Id] = $p.TotalProcessorTime.TotalSeconds }
    if ($before.Count -eq 0) { return 0.0 }
    Start-Sleep -Seconds $WallSec
    $delta = 0.0
    foreach ($p in (Get-Process -Name $Names -ErrorAction SilentlyContinue)) {
      if ($before.ContainsKey($p.Id)) { $delta += $p.TotalProcessorTime.TotalSeconds - $before[$p.Id] }
      else { return 99.0 }   # a CAD program started during the sample: someone is starting work
    }
    return $delta / $WallSec
  }

  $r1 = CadRatio
  $info.cad_ratio = [math]::Round($r1, 3)
  if ($lock -eq 'unknown') {
    if ($r1 -ge $FallbackRatio) { Out-Verdict $true 'lock state unreadable; CAD working' $info }
    Out-Verdict $false 'lock state unreadable; CAD idle' $info
  }
  if ($r1 -ge $LockedRatio) {
    $r2 = CadRatio
    $info.cad_ratio = [math]::Round([math]::Min($r1, $r2), 3)
    if ($r2 -ge $LockedRatio) { Out-Verdict $true 'locked; CAD doing sustained work' $info }
  }
  Out-Verdict $false "$lock; CAD idle" $info
} catch {
  Out-Verdict $true ('check failed: ' + $_.Exception.Message) @{ lock = 'error'; peer = ''; cad_ratio = '' }
}
