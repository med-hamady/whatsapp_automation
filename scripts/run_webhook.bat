@echo off
REM Lance le webhook Python sur 127.0.0.1:8010

cd /d "%~dp0\.."
set PYTHONPATH=%CD%\src;%CD%
python -m uvicorn whatsapp_automation.webhook.app:app --host 127.0.0.1 --port 8010 --log-level info
