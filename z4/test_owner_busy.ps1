# Branch test for owner_busy.ps1 on the workstation: simulated lock states and remote-desktop logs, and a
# hair-trigger CAD threshold. Run there: powershell -File test_owner_busy.ps1. Prints PASS/FAIL per case.
$ob = 'E:\llama-embed\owner_busy.ps1'
'[I 2026-10-26 14:00:00] Someone#1 connected.' | Set-Content $env:TEMP\fake_parsec_on.txt
"[I 2026-10-26 14:00:00] Someone#1 connected.`n[I 2026-10-26 14:05:00] Someone#1 disconnected." | Set-Content $env:TEMP\fake_parsec_off.txt
$cases = @(
  @('unlocked',  '$env:TEMP\fake_parsec_off.txt', 0.5,  'busy'),
  @('locked',    '$env:TEMP\fake_parsec_on.txt',  0.5,  'busy'),
  @('locked',    '$env:TEMP\fake_parsec_off.txt', 0.5,  'free'),
  @('locked',    '$env:TEMP\missing.txt',         0.5,  'free'),
  @('unknown',   '$env:TEMP\fake_parsec_off.txt', 0.5,  'free'),
  @('locked',    '$env:TEMP\fake_parsec_off.txt', 0.01, 'busy')
)
foreach ($c in $cases) {
  $o = & powershell -NoProfile -ExecutionPolicy Bypass -File $ob -AssumeLock $c[0] -ParsecLogs $c[1] -LockedRatio $c[2]
  $got = if ($LASTEXITCODE -eq 0) { 'busy' } else { 'free' }
  '{0} lock={1} log={2} -> {3} (want {4})  {5}' -f $(if ($got -eq $c[3]) {'PASS'} else {'FAIL'}), $c[0], (Split-Path $c[1] -Leaf), $got, $c[3], $o
}
