@echo off
title Audit Shared Drops
cd /d "%~dp0"
python "%~dp0_engine\audit_shared_drops.py"
echo.
pause
