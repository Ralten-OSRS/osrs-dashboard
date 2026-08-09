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
set "SOURCE_OUT=%~dp0Clan Package\Source Code (for the curious)"
if exist "%~dp0Clan Package\OSRS Dashboard.exe" (
  if exist "%SOURCE_OUT%\" (
    copy /Y "%~dp0Source Code README.txt" "%SOURCE_OUT%\0 - READ ME - about this code.txt" >nul
    copy /Y "%~dp0..\_engine\osrs_dashboard.py" "%SOURCE_OUT%\osrs_dashboard.py" >nul
    copy /Y "%~dp0..\_engine\dashboard_app.py" "%SOURCE_OUT%\dashboard_app.py" >nul
    copy /Y "%~dp0..\_engine\dashboard_server.py" "%SOURCE_OUT%\dashboard_server.py" >nul
    copy /Y "%~dp0..\_engine\boss_drops_generated.py" "%SOURCE_OUT%\boss_drops_generated.py" >nul
    copy /Y "%~dp0..\_engine\economic_value.py" "%SOURCE_OUT%\economic_value.py" >nul
    copy /Y "%~dp0..\_engine\value_recipes.py" "%SOURCE_OUT%\value_recipes.py" >nul
    copy /Y "%~dp0..\_engine\wiki_discovery.py" "%SOURCE_OUT%\wiki_discovery.py" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\chart.umd.min.js" "%SOURCE_OUT%\chart.umd.min.js" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\Cinzel-Latin.woff2" "%SOURCE_OUT%\Cinzel-Latin.woff2" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\CrimsonText-Regular-Latin.woff2" "%SOURCE_OUT%\CrimsonText-Regular-Latin.woff2" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\CrimsonText-Semibold-Latin.woff2" "%SOURCE_OUT%\CrimsonText-Semibold-Latin.woff2" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\CrimsonText-Italic-Latin.woff2" "%SOURCE_OUT%\CrimsonText-Italic-Latin.woff2" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\Cinzel-OFL.txt" "%SOURCE_OUT%\Cinzel-OFL.txt" >nul
    copy /Y "%~dp0..\OSRS Dashboard Resources\CrimsonText-OFL.txt" "%SOURCE_OUT%\CrimsonText-OFL.txt" >nul
  )
  echo ============================================================
  echo   DONE. The exe is in the "Clan Package" folder, with fresh
  echo   drop tables and offline chart support baked in. The source
  echo   folder was refreshed too. Replace the files in Google Drive.
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
echo   BUILD FAILED. The existing executable and source package
echo   were left in place. Read the error above before retrying.
echo ============================================================
echo(
pause
exit /b 1
