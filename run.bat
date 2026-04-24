@echo off
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
set PYTHONPATH=.
.venv\Scripts\uvicorn.exe app.main:app --reload --host 0.0.0.0 --port 8011
