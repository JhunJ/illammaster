@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Docker 없이: venv, 단위 테스트, uvicorn (DB API는 503 가능)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_all.ps1" -Thin %*
