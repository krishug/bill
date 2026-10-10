@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :install_runtime
echo Creating an isolated Python environment...
python -m venv .venv
if errorlevel 1 goto :error

:install_runtime

echo Checking the pinned runtime dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Starting the invoice extraction desktop app...
".venv\Scripts\python.exe" -m invoice_tool
if errorlevel 1 goto :error
goto :eof

:error
echo.
echo Startup failed. Verify that Python 3.11 x64 is installed and network access
echo is available for package/model downloads on the first run.
pause
exit /b 1
