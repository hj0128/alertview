@echo off
cd /d "%~dp0.."
python tools\mrblog_auth.py refresh >> tools\mrblog_refresh.log 2>&1
