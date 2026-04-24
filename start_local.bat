@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Illammaster local DB
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local.ps1"
pause
