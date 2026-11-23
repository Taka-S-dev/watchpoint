@echo off
rem Open the watchpoint demo (sample Excel, files and a local web page) in its own window.
cd /d "%~dp0.."
uv run python demo\demo.py
if errorlevel 1 pause
