@echo off
title Audit Shared Drops
cd /d "%~dp0..\_engine"
python "%~dp0..\_engine\audit_shared_drops.py"
echo.
pause
