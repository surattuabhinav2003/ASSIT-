@echo off
rem Sets CF Minutes up in this folder: a private Python environment and the packages. Safe to run again.
rem Needs Python 3.11 or newer (https://www.python.org/downloads/ or: winget install Python.Python.3.11).
setlocal
cd /d "%~dp0"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3" "python") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=%%~C"
  )
)
if not defined PY (
  echo.
  echo Python 3.11 or newer was not found.
  echo Install it with:  winget install Python.Python.3.11
  echo or from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run setup.bat again.
  exit /b 1
)
echo Using %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creating the environment...
  %PY% -m venv .venv || (echo Could not create the environment. & exit /b 1)
)
echo Installing packages ^(a few minutes the first time^)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (
  echo.
  echo Installing the packages failed. Check the internet connection and run setup.bat again.
  exit /b 1
)
".venv\Scripts\python.exe" -c "import webview, pyaudiowpatch, openai, numpy, pysilero_vad, comtypes, pystray, docx; print('All packages are in place.')" || (
  echo A package did not install correctly. Run setup.bat again.
  exit /b 1
)
echo.
echo Done. Start CF Minutes with run.bat.
exit /b 0
