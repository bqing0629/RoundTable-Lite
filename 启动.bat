@echo off
rem RoundTable Lite launcher.
rem NOTE: keep this file ASCII-only. cmd.exe parses .bat by local ANSI bytes;
rem UTF-8 Chinese text here corrupts line parsing on zh-CN systems (tested).
chcp 65001 >nul
cd /d "%~dp0"
title RoundTable Lite

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.11+ first.
  pause
  exit /b 1
)

%PY% -c "import fastapi, uvicorn, mcp" >nul 2>nul
if errorlevel 1 (
  echo First run: installing dependencies, fastapi / uvicorn / mcp ...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check your network, then run:
    echo     python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

%PY% start.py
echo.
echo Server exited. Closing this window stops RoundTable Lite.
pause
