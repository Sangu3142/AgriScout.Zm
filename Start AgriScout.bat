@echo off
rem Start the AgriScout desktop GUI with the repo's .venv.
rem Default robot address: 192.168.50.1:5000 (change it in the window if needed).
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python desktop\gui.py %*
if errorlevel 1 pause
