@echo off
setlocal
cd /d "%~dp0"
set "PORT=9100"
set "PYTHONDONTWRITEBYTECODE=1"
rem the environment lives outside Google Drive so its thousands of files are not synced
set "VENV_DIR=%LOCALAPPDATA%\webui_spain\.venv"
set "PY_CMD="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD goto :fail
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" %PY_CMD% -m venv "%VENV_DIR%"
if errorlevel 1 goto :fail
"%VENV_PY%" -c "import django" >nul 2>&1
if errorlevel 1 "%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
"%VENV_PY%" manage.py check >nul 2>&1
if errorlevel 1 "%VENV_PY%" manage.py check
if errorlevel 1 goto :fail
curl -s -o nul http://localhost:%PORT%/
if errorlevel 1 start "Spain" "%VENV_PY%" manage.py runserver 0.0.0.0:%PORT% --noreload --insecure
set "TRIES=0"
:wait
curl -s -o nul http://localhost:%PORT%/
if not errorlevel 1 goto :open
set /a TRIES+=1
if %TRIES% GEQ 60 goto :fail
ping -n 2 127.0.0.1 >nul
goto :wait
:open
start "" http://localhost:%PORT%/
exit /b 0
:fail
pause
exit /b 1
