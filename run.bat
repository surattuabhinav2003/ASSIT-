@echo off
rem Starts CF Minutes. The first time (no environment yet) it runs setup.bat by itself.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First start: setting up CF Minutes. This takes a few minutes, once.
  call "%~dp0setup.bat" || (pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" run.py
