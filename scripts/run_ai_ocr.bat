@echo off
REM Lance le service IA d'extraction sur 127.0.0.1:8008

cd /d "%~dp0\.."
set PYTHONPATH=%CD%\src;%CD%
python -m uvicorn whatsapp_automation.ai_ocr.service:app --host 127.0.0.1 --port 8008 --log-level info
