@echo off
REM Ouvre 4 fenetres PowerShell qui suivent les logs (webhook, ai_ocr, ai_ocr-http, worker)
REM en mode "live only new lines". Ctrl+C dans une fenetre arrete son tail sans toucher au service.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tail_logs.ps1"
