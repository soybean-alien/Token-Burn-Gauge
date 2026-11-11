@echo off
rem Double-click this if Claude Code cannot reach its API: it takes the live tap
rem out of the path, so new Claude Code sessions talk to the API directly again.
cd /d "%~dp0"
python "%~dp0tap_switch.py" off
pause
