@echo off
REM z4-embed-server scheduled-task action: runs the embed guard in the foreground, so the task stays
REM "running" while the guard loops. The guard starts llama-server, kills it when the preempt flag is up or
REM owner_busy.ps1 says the owner is using the machine, and restarts it after a quiet spell. Args pass
REM through (e.g. -DryRun to validate the cede, -ResumeQuietMin 10).
powershell -NoProfile -ExecutionPolicy Bypass -File E:\llama-embed\embed_guard_local.ps1 -PollSec 1.5 %*
