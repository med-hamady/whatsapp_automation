@echo off
REM Lance l'UI Flask d'annotation sur 127.0.0.1:8009

cd /d "%~dp0\.."
set PYTHONPATH=%CD%\src;%CD%
python -m whatsapp_automation.ai_ocr.annotation.app
