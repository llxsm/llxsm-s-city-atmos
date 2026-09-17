@echo off
REM ============================================================
REM  City Weather & Air Quality Data Platform - launcher
REM
REM  Starts BOTH portals in one process:
REM    analysis   -> http://127.0.0.1:8000   (business analysis)
REM    governance -> http://127.0.0.1:8001   (data governance)
REM
REM  Why .cmd instead of .ps1:
REM  Windows blocks .ps1 scripts by default (running scripts is
REM  disabled on this system), while .cmd is unaffected.
REM
REM  Usage:
REM    Double-click this file, or run:  start.cmd
REM    Analysis only:                   start.cmd --portal analysis
REM    Custom ports:                    start.cmd --port 8100 --governance-port 8101
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe reads
REM  .cmd files using the OEM code page (GBK on Chinese Windows),
REM  so UTF-8 Chinese text here would corrupt command parsing.
REM  All Chinese output comes from the Python process itself.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   [x] Virtual environment .venv not found.
  echo.
  echo   Initialize it first:
  echo       python -m venv .venv
  echo       .venv\Scripts\python.exe -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m atmos serve --portal both %*

if errorlevel 1 (
  echo.
  echo   [x] Server exited with code %errorlevel%
  pause
)
endlocal
