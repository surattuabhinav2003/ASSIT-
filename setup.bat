@echo off
rem Sets CF Minutes up in this folder: a private Python environment and the packages. Safe to run again.
rem Needs Python 3.11 or newer. If it is not installed, this offers to install it with winget.
rem To use a specific Python:  set CFM_PYTHON=C:\path\to\python.exe  before running.
setlocal enabledelayedexpansion
cd /d "%~dp0"

call :find
set "PY="
if defined CFM_PYTHON (
  "%CFM_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set PY="%CFM_PYTHON%"
)
if not defined PY for %%C in ("py -3.12" "py -3.11" "py -3" "python") do if not defined PY (
  %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=%%~C"
)
if not defined PY for %%V in (313 312 311) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
exit /b 0
