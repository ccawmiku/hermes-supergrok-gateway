@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
for /f "delims=" %%I in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"

if not defined PYTHON_EXE (
  echo Python 3.10 or newer was not found.
  exit /b 1
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(sys.version_info[:2] < (3, 10))"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)

if not exist ".build-venv\Scripts\python.exe" "%PYTHON_EXE%" -m venv .build-venv
if errorlevel 1 exit /b 1

".build-venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r build-requirements.lock
if errorlevel 1 exit /b 1

".build-venv\Scripts\pyinstaller.exe" --noconfirm --clean SuperGrokGateway.spec
if errorlevel 1 exit /b 1

echo.
echo Built: %CD%\dist\SuperGrokGateway.exe
endlocal
