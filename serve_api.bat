@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Illammaster uvicorn 8011
REM DB 없이도 /health 는 동작합니다. DB까지 쓰려면 start_local.bat
set PYTHONPATH=%~dp0
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  call .venv\Scripts\pip install -q -r requirements.txt
)
echo http://127.0.0.1:8011  ^(동일 PC^)
echo 이 PC LAN IP:8011 ^(예: http://172.23.22.63:8011^) — Windows 방화벽에서 8011 인바운드 허용
echo Cloudflare illammaster.yeobaekstudio.com 원본이 이 PC일 때 동일
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8011 --reload
pause
