@echo off
setlocal
call "%~dp0scripts\build-windows.bat"
set "build_result=%errorlevel%"
if not "%build_result%"=="0" (
    echo.
    echo Build failed. Read the error above, correct it, and run this file again.
) else (
    echo.
    echo Ready: see the output files in "%~dp0dist"
)
pause
exit /b %build_result%
