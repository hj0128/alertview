@echo off
rem Resolve project root from this script's location (tools\..) - no hardcoded path
cd /d "%~dp0.."
python tools\mrblog_auth.py refresh >> tools\mrblog_refresh.log 2>&1
