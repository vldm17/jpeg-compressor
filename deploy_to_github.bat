@echo off
chcp 65001 > nul
title Публикация на GitHub Pages
cd /d "%~dp0"

echo ========================================================
echo    Автоматическая публикация на GitHub Pages
echo ========================================================
py deploy_github.py
pause
