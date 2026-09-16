@echo off
setlocal
if not "%OS%"=="Windows_NT" (
    echo Windows builds must run on Windows.
    exit /b 1
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0windows_setup.py" %*
    exit /b
)
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0windows_setup.py" %*
    exit /b
)
echo Python 3.11 or newer was not found. Install Python with pip and the Python launcher, then retry.
exit /b 1
