@echo off
setlocal enabledelayedexpansion
title OSRS Dashboard - isolated first-run test

:: Stands up a throwaway machine so the first-run experience can actually be
:: seen. A developer machine always has a remembered account and one screenshot
:: folder, so it never enters the setup screen, the name-change question, or the
:: mixed-game-mode warning -- which is the entire path a downloader takes.
::
:: USERPROFILE and LOCALAPPDATA are redirected for this window only. Nothing
:: outside the test folder is touched: your real screenshots, favourites, XP
:: history and remembered settings are not read and not written.
::
:: Run it again any time. It rebuilds the fixture from scratch.

set "TESTHOME=%TEMP%\osrs-dashboard-testhome"
set "SHOTS=%TESTHOME%\.runelite\screenshots"

echo(
echo   Isolated first-run test
echo   ----------------------
echo   Fake home: %TESTHOME%
echo(

if exist "%TESTHOME%" (
    echo   Clearing the previous test home...
    rmdir /s /q "%TESTHOME%"
)

:: Three folders that exercise the whole flow:
::   Nameless            - the character you play now
::   Nameless Classic    - an older name, should be offered as a former name
::   Nameless-Test League - a game mode, should sit behind the disclosure and
::                          trigger the acknowledged warning when ticked
for %%F in ("Nameless" "Nameless Classic" "Nameless-Test League") do (
    mkdir "%SHOTS%\%%~F\Boss Kills" 2>nul
    mkdir "%SHOTS%\%%~F\Collection Log" 2>nul
)

:: Borrow a handful of real screenshots so the gallery and lightbox render
:: something. Copies only -- the originals are never modified.
set "REAL=%USERPROFILE%\.runelite\screenshots"
set COPIED=0
if exist "%REAL%" (
    for /f "delims=" %%P in ('dir /b /s /a-d "%REAL%\*.png" 2^>nul') do (
        if !COPIED! lss 12 (
            set /a COPIED+=1
            set "SLOT=Nameless"
            if !COPIED! gtr 4 set "SLOT=Nameless Classic"
            if !COPIED! gtr 8 set "SLOT=Nameless-Test League"
            copy /y "%%P" "%SHOTS%\!SLOT!\Boss Kills\%%~nxP" >nul 2>&1
        )
    )
)
echo   Copied !COPIED! sample screenshot(s) into the fixture.

:: Fall back to placeholders if no real screenshots were found, so the test
:: still runs on a machine that has never used RuneLite.
if !COPIED! equ 0 (
    for %%F in ("Nameless" "Nameless Classic" "Nameless-Test League") do (
        echo placeholder > "%SHOTS%\%%~F\Boss Kills\Zulrah(1) 2024-01-01_12-00-00.png"
    )
    echo   No real screenshots found - wrote placeholders instead.
)

echo(
echo   What to check:
echo     1. The setup screen lists all three folders with screenshot counts.
echo     2. "Nameless-Test League" is NOT in the main list - it is behind
echo        "Show other game modes (1)".
echo     3. Picking "Nameless" reveals "Nameless Classic" as a checkbox.
echo     4. Ticking it updates the summary to "Nameless plus 1 other folder".
echo     5. Ticking the league raises the red warning and DISABLES Build
echo        until the acknowledgement is ticked.
echo     6. In the dashboard, Settings shows the same controls and can
echo        change the composition without restarting.
echo(
pause

set "USERPROFILE=%TESTHOME%"
set "LOCALAPPDATA=%TESTHOME%\AppData\Local"
set "HOME=%TESTHOME%"

python "%~dp0..\_engine\dashboard_app.py" %*

echo(
echo   Test finished. The fake home is still at:
echo     %TESTHOME%
echo   Delete it whenever you like - nothing else was touched.
echo(
pause
endlocal
