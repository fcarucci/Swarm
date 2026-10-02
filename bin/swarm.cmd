@echo off
rem swarm CLI launcher for Windows (cmd.exe, PowerShell, Git Bash). The venv lives outside the
rem plugin, so it survives plugin updates; lib\swarm\winlaunch.py (re)builds it when
rem requirements.txt changes, then runs the swarm package from this plugin.
set "SWARM_LAUNCH=%~dp0..\lib\swarm\winlaunch.py"
where py >nul 2>nul
if errorlevel 1 goto nopy
py -3 "%SWARM_LAUNCH%" %*
exit /b %ERRORLEVEL%
:nopy
where python >nul 2>nul
if errorlevel 1 goto nopython
python "%SWARM_LAUNCH%" %*
exit /b %ERRORLEVEL%
:nopython
echo swarm: Python 3.11 or newer was not found on PATH. Install it (winget install Python.Python.3.12), then run swarm again. 1>&2
exit /b 1
