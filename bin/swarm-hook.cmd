@echo off
rem Hook entry for Claude Code and Codex on Windows: swarm-hook [--host H] start|turn|done|stop|session-start
rem (hook JSON on stdin). Never fails the agent; see lib\swarm\winhook.py.
set "SWARM_HOOK=%~dp0..\lib\swarm\winhook.py"
if defined SWARM_VENV (set "SWARM_VPY=%SWARM_VENV%\Scripts\python.exe") else (set "SWARM_VPY=%USERPROFILE%\.local\share\swarm\venv\Scripts\python.exe")
if exist "%SWARM_VPY%" goto venv
where py >nul 2>nul
if errorlevel 1 goto nopy
py -3 "%SWARM_HOOK%" %*
exit /b 0
:nopy
where python >nul 2>nul
if errorlevel 1 goto none
python "%SWARM_HOOK%" %*
exit /b 0
:venv
"%SWARM_VPY%" "%SWARM_HOOK%" %*
exit /b 0
:none
more >nul
exit /b 0
