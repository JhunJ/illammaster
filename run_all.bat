@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 전체: Docker/Podman으로 PostGIS -> alembic -> pytest(통합 포함) -> uvicorn
REM Docker 없으면 run_quick.bat 또는 run_all.bat -Thin
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_all.ps1" %*
