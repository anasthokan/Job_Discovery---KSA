@echo off
cd /d "%~dp0"
:again
python -m uvicorn api:app --host 127.0.0.1 --port 9002
echo [%date% %time%] API stopped. Starting again in 5 seconds.
timeout /t 5 /nobreak >nul
goto again
