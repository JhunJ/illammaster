@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Illammaster dev
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_dev.ps1"
pause
