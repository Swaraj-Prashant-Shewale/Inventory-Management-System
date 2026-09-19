@echo off
rem ============================================================
rem  COMPLETELY LOCAL sandbox.
rem  Forces a local SQLite file (desktop_sandbox.db in this
rem  folder) and NEVER touches Supabase / the live cloud DB.
rem  Click around freely — nothing here affects real data.
rem  The first launch asks you to create a local Owner account.
rem ============================================================
cd /d "%~dp0"
set "DB_BACKEND=sqlite"
set "SQLITE_FILE=desktop_sandbox.db"
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
