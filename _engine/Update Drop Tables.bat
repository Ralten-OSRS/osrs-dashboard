@echo off
title Update OSRS Drop Tables
cd /d "%~dp0.."
python "%~dp0update_boss_drops.py"
echo.
if %errorlevel% neq 0 (
    echo ── Update failed. See error above. ──
    pause
    exit /b 1
)
echo ── Drop tables updated. Refresh the dashboard to see new drops. ──
pause
