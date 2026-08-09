@echo off
REM ============================================================
REM   Build the OSRS Dashboard clan .exe  (run this on Windows)
REM   Output lands in the "Clan Package" folder next to this file.
REM   Step 1 refreshes drop tables from the wiki; Step 2 compiles.
REM ============================================================
setlocal
cd /d "%~dp0..\_engine"

echo(
echo Step 1 of 2: refreshing drop tables from the OSRS Wiki...
echo   (If the wiki is down or this hiccups, the build just continues
echo    with the drop tables from your last run - no harm done.)
echo(
if /I "%~1"=="--skip-wiki" (
  echo   Skipping wiki refresh for this code-only rebuild.
  goto :after_wiki
)
python update_boss_drops.py
if errorlevel 1 (
  echo(
  echo   NOTE: drop-table refresh was incomplete. The verified existing
  echo   tables were preserved and will be used for this build.
)

:after_wiki
echo(
echo Step 2 of 2: building "OSRS Dashboard.exe" - this takes a minute or two...
echo(
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller ^(one-time^)...
  python -m pip install --user pyinstaller
  if errorlevel 1 goto :build_failed
)
python -m PyInstaller --onefile --console ^
  --name "OSRS Dashboard" ^
  --hidden-import version ^
  --hidden-import console ^
  --hidden-import settings ^
  --hidden-import osrs_dashboard ^
  --hidden-import dashboard_server ^
  --hidden-import boss_drops_generated ^
  --hidden-import economic_value ^
  --hidden-import value_recipes ^
  --hidden-import wiki_discovery ^
  --add-data "%~dp0..\OSRS Dashboard Resources\chart.umd.min.js;." ^
  --add-data "%~dp0..\OSRS Dashboard Resources\Cinzel-Latin.woff2;." ^
  --add-data "%~dp0..\OSRS Dashboard Resources\CrimsonText-Regular-Latin.woff2;." ^
  --add-data "%~dp0..\OSRS Dashboard Resources\CrimsonText-Semibold-Latin.woff2;." ^
  --add-data "%~dp0..\OSRS Dashboard Resources\CrimsonText-Italic-Latin.woff2;." ^
  --distpath "%~dp0Clan Package" ^
  --workpath "%~dp0_build" ^
  --specpath "%~dp0_build" ^
  dashboard_app.py
if errorlevel 1 goto :build_failed

echo(
if exist "%~dp0Clan Package\OSRS Dashboard.exe" (
  echo ============================================================
  echo   DONE. The exe is in the "Clan Package" folder, with fresh
  echo   drop tables and offline chart support baked in.
  echo(
  echo   TO RELEASE IT:
  echo     1. Commit and push your code changes in GitHub Desktop.
  echo     2. Draft a new release at
  echo        https://github.com/Ralten-OSRS/osrs-dashboard/releases
  echo     3. Attach this exe and publish.
  echo(
  echo   GitHub attaches the matching source automatically, so there
  echo   is no source folder to refresh by hand anymore.
  echo ============================================================
) else (
  echo Build did NOT produce an exe - scroll up to read the error.
)
echo(
pause
exit /b 0

:build_failed
echo(
echo ============================================================
echo   BUILD FAILED. The existing executable was left in place.
echo   Read the error above before retrying.
echo ============================================================
echo(
pause
exit /b 1
