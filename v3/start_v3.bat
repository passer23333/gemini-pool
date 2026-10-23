@echo off
REM GeminiPool v3 - gateway only (no tray icon, no desktop window).
REM For the full desktop shell (window + tray + watchdog) use start-app.bat.
REM NOTE: keep this file pure ASCII - CJK in .bat breaks under the default OEM codepage.
REM The exe has a CJK filename, so it is never written literally here.

cd /d "%~dp0"
setlocal enabledelayedexpansion

set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" set "PY=python"
set "PROBE=%~dp0tools\probe_port.py"
set "SLEEP=%SystemRoot%\System32\timeout.exe"

echo ==========================================
echo   GeminiPool v3  -  gateway only
echo ==========================================
echo.

REM --- 1) Already running? Just open the console. -------------------------
"%PY%" -u "%PROBE%" --open
if not errorlevel 1 (
  echo.
  echo [ok] Existing gateway detected - console opened.
  exit /b 0
)

REM --- 2) Start the gateway in its own minimized window -------------------
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo [..] Starting gateway with %PY%

start "GeminiPool gateway" /min "%PY%" -u "%~dp0server.py"

REM --- 3) Wait until it answers, then open the browser on the real port ---
for /l %%i in (1,1,40) do (
  "%SLEEP%" /t 1 /nobreak >nul 2>&1
  "%PY%" -u "%PROBE%" --open
  if not errorlevel 1 (
    echo.
    echo [ok] Gateway is ready.
    echo.
    echo      It runs in the minimized "GeminiPool gateway" window.
    echo      Closing THIS window does NOT stop it. To stop the gateway,
    echo      restore that window and press Ctrl+C.
    echo.
    exit /b 0
  )
)

echo [!!] Gateway did not answer within 40 seconds.
echo      Check the minimized "GeminiPool gateway" window, or logs\ for details.
pause
exit /b 1
