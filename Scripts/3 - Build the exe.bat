@echo off
REM ============================================================
REM   Build the OSRS Dashboard .exe  (run this on Windows)
REM   Step 1 refreshes drop tables from the wiki; Step 2 compiles.
REM   The finished exe lands in the project's "Build Output" folder,
REM   next to Scripts. The log lands beside it as build-log.txt.
REM ============================================================
setlocal

set "PROJECT=%~dp0.."
set "ENGINE=%PROJECT%\_engine"
set "RESOURCES=%PROJECT%\OSRS Dashboard Resources"
set "BUILDDIR=%RESOURCES%\Build"
set "OUTDIR=%PROJECT%\Build Output"
set "BUILDLOG=%OUTDIR%\build-log.txt"

if not exist "%BUILDDIR%\_build" mkdir "%BUILDDIR%\_build" >nul 2>&1
if not exist "%OUTDIR%" mkdir "%OUTDIR%" >nul 2>&1

echo Build started %DATE% %TIME%> "%BUILDLOG%"
echo.>> "%BUILDLOG%"

echo(
echo Checking inputs...

REM Fail on a missing input with a readable line naming it, rather than letting
REM PyInstaller surface it as a traceback three hundred lines into the log.
set "MISSING="
call :require "%ENGINE%\dashboard_app.py"
call :require "%ENGINE%\icon.ico"
call :require "%RESOURCES%\chart.umd.min.js"
call :require "%RESOURCES%\Cinzel-Latin.woff2"
call :require "%RESOURCES%\CrimsonText-Regular-Latin.woff2"
call :require "%RESOURCES%\CrimsonText-Semibold-Latin.woff2"
call :require "%RESOURCES%\CrimsonText-Italic-Latin.woff2"
if defined MISSING goto :missing_inputs

REM The icon is copied next to the spec on purpose. PyInstaller resolves BOTH
REM --icon and --add-data against the .spec file's folder, not the working
REM directory -- and it writes --icon into the spec as a single-quoted Python
REM string without escaping it, so an absolute path is not an option either:
REM this project lives under "Kyle's Workspace", and that apostrophe ends the
REM string early and the spec fails to compile. Copying the icon into _build
REM and naming it plainly sidesteps both problems and does not depend on how
REM deep this script happens to sit. Three consecutive failed builds on
REM 2026-08-10 were this, so do not "tidy" it back into a path.
copy /y "%ENGINE%\icon.ico" "%BUILDDIR%\_build\icon.ico" >nul
if errorlevel 1 (
  echo   Could not copy the icon into the build folder.
  echo Could not copy icon.ico into _build>> "%BUILDLOG%"
  goto :build_failed
)
echo   All inputs present.

cd /d "%ENGINE%"

echo(
echo Step 1 of 2: refreshing drop tables from the OSRS Wiki...
echo   About 70 bosses, one wiki request each. Takes 2-4 minutes.
echo   Progress prints below - it is not stuck. Do not close this window.
echo   (If the wiki is down or this hiccups, the build just continues
echo    with the drop tables from your last run - no harm done.)
echo   Pass --skip-wiki to skip this entirely on a code-only rebuild.
echo(
echo ==== STEP 1: drop tables ====>> "%BUILDLOG%"
if /I "%~1"=="--skip-wiki" (
  echo   Skipping wiki refresh for this code-only rebuild.
  echo Skipped by --skip-wiki>> "%BUILDLOG%"
  goto :after_wiki
)
REM Tee rather than redirect. Sending this to the log alone left the window
REM blank for minutes and it read as a hang -- it was killed mid-run once
REM because of it. $env:BUILDLOG is used instead of interpolating the path
REM into the PowerShell string, because the path contains an apostrophe
REM ("Kyle's Workspace") and would end the string early.
powershell -NoProfile -ExecutionPolicy Bypass -Command "python update_boss_drops.py 2>&1 | Tee-Object -FilePath $env:BUILDLOG -Append"
if errorlevel 1 (
  echo(
  echo   NOTE: drop-table refresh was incomplete. The verified existing
  echo   tables were preserved and will be used for this build.
)

:after_wiki
echo(
echo Step 2 of 2: building "OSRS Dashboard.exe"...
echo   This one IS silent for a minute or two - PyInstaller writes to
echo   build-log.txt rather than here. Nothing will print until it is
echo   done, and that is expected. Do not close this window.
echo(
echo.>> "%BUILDLOG%"
echo ==== STEP 2: PyInstaller ====>> "%BUILDLOG%"

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller ^(one-time^)...
  python -m pip install --user pyinstaller >> "%BUILDLOG%" 2>&1
  if errorlevel 1 goto :build_failed
)

:: --windowed, not --console. A black terminal reads as something malicious to
:: a non-technical player, especially straight after SmartScreen. Consequences,
:: all handled and all easy to break again:
::   - sys.stdout is None, so ALL output must go through console.py.
::   - stdin does not exist, so nothing in the launcher may call input().
::   - the first run asks which character in the browser, not the terminal.
::   - errors surface on the local page, in the log beside the screenshots,
::     and via console.alert() when the service itself cannot start.
:: Before touching this line, read DESIGN.md and GitHub issue #2.
python -m PyInstaller --onefile --windowed ^
  --name "OSRS Dashboard" ^
  --icon "icon.ico" ^
  --hidden-import version ^
  --hidden-import console ^
  --hidden-import settings ^
  --hidden-import osrs_dashboard ^
  --hidden-import dashboard_server ^
  --hidden-import boss_drops_generated ^
  --hidden-import economic_value ^
  --hidden-import value_recipes ^
  --hidden-import wiki_discovery ^
  --add-data "%RESOURCES%\chart.umd.min.js;." ^
  --add-data "%RESOURCES%\Cinzel-Latin.woff2;." ^
  --add-data "%RESOURCES%\CrimsonText-Regular-Latin.woff2;." ^
  --add-data "%RESOURCES%\CrimsonText-Semibold-Latin.woff2;." ^
  --add-data "%RESOURCES%\CrimsonText-Italic-Latin.woff2;." ^
  --distpath "%OUTDIR%" ^
  --workpath "%BUILDDIR%\_build" ^
  --specpath "%BUILDDIR%\_build" ^
  dashboard_app.py >> "%BUILDLOG%" 2>&1
if errorlevel 1 goto :build_failed

echo(
if exist "%OUTDIR%\OSRS Dashboard.exe" (
  echo ============================================================
  echo   DONE. Fresh drop tables and offline chart support baked in.
  echo(
  echo   The exe is at:
  echo     Build Output\OSRS Dashboard.exe
  echo(
  dir /tc "%OUTDIR%\OSRS Dashboard.exe" | findstr /i "OSRS"
  echo(
  echo   Check that timestamp is from just now. A build can report
  echo   success while writing somewhere unexpected, and a stale exe
  echo   looks identical to a fresh one.
  echo(
  echo   TO RELEASE IT:
  echo     1. Commit and push your code changes in GitHub Desktop.
  echo     2. Draft a new release at
  echo        https://github.com/Ralten-OSRS/osrs-dashboard/releases
  echo     3. Attach this exe and publish.
  echo ============================================================
) else (
  echo   PyInstaller reported success but produced no exe.
  echo PyInstaller exited 0 but no exe was produced>> "%BUILDLOG%"
  goto :build_failed
)
echo(
pause
exit /b 0

:require
if not exist "%~1" (
  echo   MISSING: %~1
  echo MISSING INPUT: %~1>> "%BUILDLOG%"
  set "MISSING=1"
)
exit /b 0

:missing_inputs
echo(
echo ============================================================
echo   BUILD NOT STARTED - a required file is missing.
echo   The missing paths are listed above and in:
echo     %BUILDLOG%
echo ============================================================
echo(
pause
exit /b 1

:build_failed
echo(
echo ============================================================
echo   BUILD FAILED. The existing executable was left in place.
echo(
echo   The full error is in:
echo     %BUILDLOG%
echo(
echo   Last 25 lines:
echo ------------------------------------------------------------
powershell -NoProfile -Command "if (Test-Path -LiteralPath $env:BUILDLOG) { Get-Content -LiteralPath $env:BUILDLOG -Tail 25 }" 2>nul
echo ------------------------------------------------------------
echo   Attach build-log.txt if you need someone to look at it.
echo ============================================================
echo(
pause
exit /b 1
