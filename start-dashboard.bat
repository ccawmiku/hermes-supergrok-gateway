@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="

for /f "delims=" %%I in ('where python 2^>nul') do (
  if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
)

if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"

if not defined PYTHON_EXE (
  echo Python 3.10 or newer was not found.
  echo Install Python from https://www.python.org/downloads/ and enable "Add Python to PATH".
  goto :error
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(sys.version_info[:2] < (3, 10))"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  "%PYTHON_EXE%" --version
  goto :error
)

for /f "delims=" %%V in ('"%PYTHON_EXE%" --version 2^>^&1') do echo Using %%V

if /i "%~1"=="--diagnose" (
  echo Python detection is ready.
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo Preparing local environment...
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e .
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m supergrok_openai serve --host 0.0.0.0 --allow-network %*
goto :end

:error
echo.
echo Could not start SuperGrok OpenAI. Press any key to close.
pause >nul

:end
endlocal
