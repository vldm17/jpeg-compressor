@echo off
chcp 65001 > nul
title JPEG Compressor & Artifact Studio
cd /d "%~dp0"

echo ======================================================
echo    JPEG Compressor ^& Artifact Studio
echo ======================================================
echo Запуск приложения...

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py app.py
    goto end
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python app.py
    goto end
)

echo.
echo [ОШИБКА] Python не обнаружен в системе!
echo Пожалуйста, установите Python с сайта https://python.org
pause

:end
