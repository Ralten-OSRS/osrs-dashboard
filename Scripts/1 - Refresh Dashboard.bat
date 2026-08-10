@echo off
title Refresh OSRS Dashboard
cd /d "%~dp0.."

:: Runs the same launcher the packaged app runs, so the maintainer hits the
:: same startup path, the same remembered-account handling, and the same
:: failure modes as anyone who downloads a release. Running from source keeps
:: config.py; the packaged build resets personalization to neutral.
::
:: Pass --pick to choose a different character, or --refresh-boss-data to
:: re-read every boss drop table from the wiki before building.
python "%~dp0..\_engine\dashboard_app.py" %*
if %errorlevel% neq 0 (
    echo.
    echo -- Script failed. See error above. --
    pause
    exit /b 1
)
