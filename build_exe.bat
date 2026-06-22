@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set LOG=build_log.txt

echo ============================================
echo  SWR Analysis Tool v1.0.0 - EXE Builder
echo ============================================
echo.
echo This window will stay open if something fails.
echo A build log will be written to: %LOG%
echo.

echo Build started %date% %time% > "%LOG%"

set PY=py -3
%PY% --version >> "%LOG%" 2>&1
if errorlevel 1 (
    set PY=python
)

%PY% --version
if errorlevel 1 (
    echo.
    echo ERROR: Python was not found.
    echo Install Python, or check "Add Python to PATH".
    echo See %LOG% for details.
    pause
    exit /b 1
)

%PY% -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL

%PY% -m pip install pyinstaller pyserial matplotlib >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "SWR Analysis Tool.spec" del "SWR Analysis Tool.spec"

%PY% -m PyInstaller --onefile --windowed --clean --noconfirm --name "SWR Analysis Tool" --icon "swr_analysis_tool.ico" --add-data "swr_analysis_tool.ico;." swr_analysis_tool.py >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL

if not exist "dist\SWR Analysis Tool.exe" (
    echo.
    echo ERROR: Build finished but EXE was not found.
    echo See %LOG% for details.
    pause
    exit /b 1
)

echo.
echo Build complete.
echo EXE location:
echo   dist\SWR Analysis Tool.exe
echo.
pause
exit /b 0

:BUILD_FAIL
echo.
echo ERROR: Build failed.
echo Open build_log.txt in this folder and send me the last 20 lines.
echo.
pause
exit /b 1
