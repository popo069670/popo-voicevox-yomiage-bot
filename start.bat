@echo off
cd /d "%~dp0"
:loop
venv\Scripts\python.exe bot.py
echo Bot stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
