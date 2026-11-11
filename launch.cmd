@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not defined BEAMDIAG_LAUNCH_MODE set "BEAMDIAG_LAUNCH_MODE=simulate"
if defined BEAMDIAG_PYTHON goto explicitPython
if not exist "%~dp0.venv\Scripts\python.exe" goto findPython
"%~dp0.venv\Scripts\python.exe" -I -c "import sys,struct,sysconfig;sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,15) and struct.calcsize('P')==8 and sysconfig.get_platform()=='win-amd64' else 1)" >nul 2>&1
if errorlevel 1 goto findPython
"%~dp0.venv\Scripts\python.exe" "%~dp0bootstrap.py" --mode "%BEAMDIAG_LAUNCH_MODE%" %*
exit /b %ERRORLEVEL%
:findPython
where py >nul 2>&1
if errorlevel 1 goto pathPython
for %%V in (3.12 3.13 3.14 3.11) do (
    py -%%V -I -c "import sys,struct,sysconfig;sys.exit(0 if struct.calcsize('P')==8 and sysconfig.get_platform()=='win-amd64' else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "BEAMDIAG_SELECTED_PYTHON=%%V"
        goto usePy
    )
)
:pathPython
python -I -c "import sys,struct,sysconfig;sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,15) and struct.calcsize('P')==8 and sysconfig.get_platform()=='win-amd64' else 1)" >nul 2>&1
if errorlevel 1 goto missingPython
python "%~dp0bootstrap.py" --mode "%BEAMDIAG_LAUNCH_MODE%" %*
exit /b %ERRORLEVEL%
:usePy
py -%BEAMDIAG_SELECTED_PYTHON% "%~dp0bootstrap.py" --mode "%BEAMDIAG_LAUNCH_MODE%" %*
exit /b %ERRORLEVEL%
:explicitPython
"%BEAMDIAG_PYTHON%" "%~dp0bootstrap.py" --mode "%BEAMDIAG_LAUNCH_MODE%" %*
exit /b %ERRORLEVEL%
:missingPython
echo Install standard x64 Python 3.11-3.14 from python.org, then run setup.cmd.
echo Python 3.12 is recommended. Install the Python launcher or add Python to PATH.
echo You can also set BEAMDIAG_PYTHON to the full path of a supported python.exe.
echo See WINDOWS_SETUP.md. No PowerShell execution-policy change is needed.
exit /b 1
