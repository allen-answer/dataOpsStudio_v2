@echo off
setlocal enableextensions
rem DataOpsStudio 2.0 - Win10 x64 offline launcher.
rem First run: build the local venv, generate secrets, init PG, migrate, create
rem an admin user. Subsequent runs: just start API + worker + PG.

set "BUNDLE=%~dp0"
if "%BUNDLE:~-1%"=="\" set "BUNDLE=%BUNDLE:~0,-1%"

if not defined DATAOPS_HOME set "DATAOPS_HOME=%BUNDLE%\home"
if not defined DATAOPS_API_PORT set "DATAOPS_API_PORT=8020"
if not defined DATAOPS_PG_PORT set "DATAOPS_PG_PORT=15432"
if not defined DATAOPS_ADMIN_USER set "DATAOPS_ADMIN_USER=admin"

set "PYDIR=%BUNDLE%\runtime\python"
set "UVEXE=%BUNDLE%\runtime\uv\uv.exe"
set "VENV=%BUNDLE%\.venv"
set "VENVPY=%VENV%\Scripts\python.exe"
set "PGBIN=%BUNDLE%\pgsql\bin"
set "WHEELS=%BUNDLE%\runtime\wheels"
set "REQS=%BUNDLE%\runtime\requirements-frozen.txt"

rem Offline, no-sync: uv only ever execs the prebuilt project .venv (bundle root
rem holds pyproject.toml, so uv run resolves it), never the network.
set "UV_OFFLINE=1"
set "UV_NO_SYNC=1"
set "UV_FROZEN=1"
set "UV_PYTHON_DOWNLOADS=0"
set "DATAOPS_FORM=portable"
set "DATAOPS_FRONTEND_DIST=%BUNDLE%\frontend\dist"
set "PATH=%PGBIN%;%PATH%"


if not exist "%VENVPY%" (
  echo [start] first run: building offline Python environment ...
  "%PYDIR%\python.exe" -m venv "%VENV%"
  if errorlevel 1 goto :fail
  "%VENVPY%" -m pip install --no-index --find-links "%WHEELS%" --require-hashes -r "%REQS%"
  if errorlevel 1 goto :fail
)

if not exist "%DATAOPS_HOME%\config\.secret_master.key" (
  echo [start] first run: generating bootstrap secrets ...
  call :launcher bootstrap init || goto :fail
  echo [start] first run: starting metadata PostgreSQL ...
  call :launcher pg-up || goto :fail
  echo [start] first run: applying database migrations ...
  call :launcher alembic-up || goto :fail
  echo [start] first run: creating admin user "%DATAOPS_ADMIN_USER%"
  if defined DATAOPS_ADMIN_PASSWORD (
    call :launcher admin create --username "%DATAOPS_ADMIN_USER%" --password "%DATAOPS_ADMIN_PASSWORD%" || goto :fail
  ) else (
    call :launcher admin create --username "%DATAOPS_ADMIN_USER%" || goto :fail
  )
)

echo [start] launching DataOpsStudio on http://127.0.0.1:%DATAOPS_API_PORT%/
echo [start] press Ctrl+C in this window to stop gracefully.
call :launcher up
goto :eof

:launcher
"%VENVPY%" -m app.launcher --root "%DATAOPS_HOME%" --pg-bin-dir "%PGBIN%" --uv "%UVEXE%" %*
exit /b %errorlevel%

:fail
echo [start] ERROR: startup failed (exit %errorlevel%). See %DATAOPS_HOME%\logs\ for details.
exit /b 1
