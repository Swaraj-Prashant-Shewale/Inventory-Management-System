@echo off
rem Starts the web app locally with the project's own Python (.venv).
rem Then open http://127.0.0.1:8000 in your browser. Press Ctrl+C here to stop it.
cd /d "%~dp0"
".venv\Scripts\python.exe" run_web.py
pause
