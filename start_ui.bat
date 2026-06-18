@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="

where py >nul 2>nul
if %errorlevel%==0 (
    py dub_to_norwegian.py --ui
    pause
    exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
    python dub_to_norwegian.py --ui
    pause
    exit /b %errorlevel%
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%~D\python.exe" set "PYTHON_EXE=%%~D\python.exe"
)

if not defined PYTHON_EXE (
    for /d %%D in ("%ProgramFiles%\Python*") do (
        if exist "%%~D\python.exe" set "PYTHON_EXE=%%~D\python.exe"
    )
)

if not defined PYTHON_EXE (
    for /d %%D in ("%ProgramFiles(x86)%\Python*") do (
        if exist "%%~D\python.exe" set "PYTHON_EXE=%%~D\python.exe"
    )
)

if defined PYTHON_EXE (
    echo Found Python at: %PYTHON_EXE%
    "%PYTHON_EXE%" dub_to_norwegian.py --ui
    pause
    exit /b %errorlevel%
)

echo Could not find Python.
echo.
echo Install Python from https://www.python.org/downloads/windows/
echo IMPORTANT: tick "Add python.exe to PATH" during installation.
echo Then run install_dependencies.bat and start_ui.bat again.
pause
exit /b 1
