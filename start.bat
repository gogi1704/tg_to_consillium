@echo off
cd /d %~dp0
title Consilium Telegram Bot

if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -u bot.py
  goto finished
)

if exist ..\ai_project\.venv\Scripts\python.exe (
  ..\ai_project\.venv\Scripts\python.exe -u bot.py
  goto finished
)

where python >nul 2>nul
if not errorlevel 1 (
  python -u bot.py
  goto finished
)

echo Python 3.11+ was not found.
echo Install Python or create .venv in this folder.
pause
exit /b 1

:finished
if errorlevel 1 pause
