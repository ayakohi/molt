@echo off
rem Opens the molt menu: pick a thread, then trim it or make a readable copy.
chcp 65001 >nul
python "%~dp0roll.py"
echo.
pause
