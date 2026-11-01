@echo off
chcp 65001 > nul
title JPEG Compressor - Доступ из интернета (Cloudflare Tunnel)
cd /d "%~dp0"

echo Запуск сервера с созданием публичной ссылки в интернет...
py server.py --online
if %ERRORLEVEL% NEQ 0 (
    python server.py --online
)
pause
