@echo off
REM Lance un worker. Lancer plusieurs fois ce .bat pour avoir N workers.

cd /d "%~dp0\.."
set PYTHONPATH=%CD%\src;%CD%
python -m whatsapp_automation.worker.main
