@echo off
rem Token-burn gauge launcher (Task Scheduler target). No logic lives here.
rem Each program exits on its own if its port is already taken, so running this twice is safe.
cd /d "%~dp0"
start "" pythonw "%~dp0collector_b.py"
start "" pythonw "%~dp0proxy.py"

start "" pythonw "%~dp0tap.py"
