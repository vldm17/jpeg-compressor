@echo off
chcp 65001 > nul
title JPEG Compressor - Доступ по домашнему Wi-Fi
cd /d "%~dp0"

echo Запуск сервера для доступа с телефона и ПК...
py server.py
if %ERRORLEVEL% NEQ 0 (
    python server.py
)
pause
