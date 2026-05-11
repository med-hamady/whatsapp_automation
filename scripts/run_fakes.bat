@echo off
REM Lance les 3 fake servers en parallele (UCRM 9001, MikroTik 9002, UltraMsg 9003).

cd /d "%~dp0\.."
set PYTHONPATH=%CD%\src;%CD%
start "fake-ucrm"      cmd /k "python -m fakes.fake_ucrm"
start "fake-mikrotik"  cmd /k "python -m fakes.fake_mikrotik"
start "fake-ultramsg"  cmd /k "python -m fakes.fake_ultramsg"
echo Fake servers lances : 9001/9002/9003
