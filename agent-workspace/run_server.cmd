@echo off
rem Portable launcher for robot-motion-mcp inside the agent workspace.
rem Resolves all paths relative to this file; no machine-specific absolute paths here.
setlocal
set "HERE=%~dp0"
set "PROJECT_ROOT=%HERE%.."
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
"%PYTHON%" -m robot_motion_mcp.server
