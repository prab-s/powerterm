@echo off
setlocal
if not "%OS%"=="Windows_NT" (
    echo Windows builds must run on Windows.
    exit /b 1
)
python "%~dp0build.py" %*
exit /b %errorlevel%
