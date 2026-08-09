@echo off
title Refresh OSRS Dashboard
cd /d "%~dp0"
python "%~dp0_engine\dashboard_server.py"
if %errorlevel% neq 0 (
    echo.
    echo -- Script failed. See error above. --
    pause
    exit /b 1
)
