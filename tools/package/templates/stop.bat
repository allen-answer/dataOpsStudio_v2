@echo off
setlocal enableextensions
set "BUNDLE=%~dp0"
if "%BUNDLE:~-1%"=="\" set "BUNDLE=%BUNDLE:~0,-1%"
if not defined DATAOPS_HOME set "DATAOPS_HOME=%BUNDLE%\home"
set "VENVPY=%BUNDLE%\.venv\Scripts\python.exe"
set "PGBIN=%BUNDLE%\pgsql\bin"
set "UVEXE=%BUNDLE%\runtime\uv\uv.exe"
set "PATH=%PGBIN%;%PATH%"
echo [stop] stopping DataOpsStudio (graceful; active queries drain) ...
"%VENVPY%" -m app.launcher --root "%DATAOPS_HOME%" --pg-bin-dir "%PGBIN%" --uv "%UVEXE%" stop %*
