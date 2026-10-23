@echo off
REM GeminiPool v3 launcher
REM NOTE: keep this file pure ASCII. The exe has a CJK filename, so it is
REM resolved by wildcard enumeration instead of being written literally here.
REM
REM Gateway detection is done by tools\probe_port.py, not curl: a system-wide
REM HTTP_PROXY hijacks curl even for 127.0.0.1 and made a live gateway look
REM dead, so this script used to always fall through to "start the exe".

cd /d "%~dp0"
setlocal enabledelayedexpansion

set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" set "PY=python"
set "PROBE=%~dp0tools\probe_port.py"
set "SLEEP=%SystemRoot%\System32\timeout.exe"
set "EXE="

echo ==========================================
echo   GeminiPool v3  -  Gemini Account Pool
echo ==========================================
echo.

if not exist "%~dp0logs" mkdir "%~dp0logs"

REM --- 1) Already running? Just open the console. -------------------------
"%PY%" -u "%PROBE%" --open
if not errorlevel 1 (
  echo [ok] Gateway already responding - console opened.
  exit /b 0
)

REM --- 2) Prefer the desktop shell (window + tray + watchdog) ------------
REM The tilde-f modifier on the loop variable strips the quotes that the
REM wildcard match keeps; without it the start line gets doubled quotes
REM around the path. Do not spell the modifier out here - cmd expands
REM percent sequences even inside a REM line and fails with a syntax error.
for %%f in ("%~dp0runtime\*.exe") do set "EXE=%%~f"

if defined EXE (
  echo [..] Starting desktop shell: !EXE!
  echo      Waiting for the gateway to come up...
  start "" "!EXE!"

  for /l %%i in (1,1,40) do (
    "%SLEEP%" /t 1 /nobreak >nul 2>&1
    "%PY%" -u "%PROBE%" --open
    if not errorlevel 1 (
      echo [ok] Ready.
      exit /b 0
    )
  )

  echo [!!] The shell did not report a healthy gateway within 40 seconds.
  echo      If nothing appeared at all, the exe failed to start on this PC.
  REM No parentheses in this echo: an unescaped close-paren inside an
REM if-block ends the block early and cmd dies with a syntax error.
echo      Falling back to headless mode - gateway only, no tray.
  echo      See logs\electron.log for details.
  echo.
)

REM --- 3) Fallback: plain gateway, no tray --------------------------------
echo [..] Starting gateway with %PY%

start "GeminiPool gateway" /min "%PY%" -u "%~dp0server.py"

for /l %%i in (1,1,30) do (
  "%SLEEP%" /t 1 /nobreak >nul 2>&1
  "%PY%" -u "%PROBE%" --open
  if not errorlevel 1 (
    echo [ok] Ready.
    exit /b 0
  )
)

echo [!!] Gateway did not start. Check logs\ for details.
pause
exit /b 1
