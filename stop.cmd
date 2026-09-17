@echo off
REM ============================================================
REM  City Weather & Air Quality Data Asset Platform - stopper
REM
REM  Ends every "python -m atmos serve" process (a venv on Windows
REM  runs as a launcher + real interpreter pair, so both must go).
REM
REM  This file is intentionally ASCII-only; see start.cmd for why.
REM ============================================================
setlocal

echo.
echo   Looking for running platform server...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m atmos serve*' }); if ($procs.Count -eq 0) { Write-Host '  No running server found.'; exit 0 }; foreach ($p in $procs) { Write-Host ('  stopping PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 800; $left = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m atmos serve*' }); if ($left.Count -gt 0) { Write-Host '  [!] Still running, stop manually:'; foreach ($p in $left) { Write-Host ('      taskkill /F /PID ' + $p.ProcessId) } } else { Write-Host '  Server stopped.' }"

echo.
endlocal
