@echo off
REM ============================================================
REM   Build the OSRS Dashboard .exe  (run this on Windows)
REM   Step 1 refreshes drop tables from the wiki; Step 2 compiles.
REM   The finished exe lands in:
REM     OSRS Dashboard Resources\Build\Output\
REM ============================================================
setlocal

REM Every path this script needs, resolved once from its own location.
REM They are named rather than repeated inline because the script has moved
REM folders before, and a half-updated relative path fails by writing the exe
REM somewhere unexpected while still reporting success.
set "PROJECT=%~dp0.."
set "ENGINE=%PROJECT%\_engine"
set "RESOURCES=%PROJECT%\OSRS Dashboard Resources"
set "BUILDDIR=%RESOURCES%\Build"

cd /d "%ENGINE%"

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
  --icon "%ENGINE%\icon.ico" ^
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
  --distpath "%BUILDDIR%\Output" ^
  --workpath "%BUILDDIR%\_build" ^
  --specpath "%BUILDDIR%\_build" ^
  dashboard_app.py
if errorlevel 1 goto :build_failed

echo(
if exist "%BUILDDIR%\Output\OSRS Dashboard.exe" (
  echo ============================================================
  echo   DONE. Fresh drop tables and offline chart support baked in.
  echo   The exe is at:
  echo     OSRS Dashboard Resources\Build\Output\OSRS Dashboard.exe
  echo   Check its timestamp is from just now before releasing it.
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
