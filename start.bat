@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)"
    if errorlevel 1 (
        echo Python 3.11 x64 is required. Install it and ensure "python" points to it.
        goto :error
    )
    echo Creating an isolated Python environment...
    python -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 (
    echo The existing .venv was not created with Python 3.11. Recreate it with Python 3.11 x64.
    goto :error
)

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
