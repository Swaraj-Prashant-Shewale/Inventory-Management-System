@echo off
rem ============================================================
rem  LIVE data. Uses whatever your .env says — currently the
rem  live Supabase cloud database (public schema). Anything you
rem  do here is REAL and permanent. Use the LOCAL sandbox
rem  launcher instead if you just want to try the app.
rem ------------------------------------------------------------
rem  (Runs with the project's own Python in .venv, which has all
rem  the packages. Opening main.py directly uses the system
rem  Python and fails with "ModuleNotFoundError".)
rem ============================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
