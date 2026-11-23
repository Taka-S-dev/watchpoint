@echo off
rem Delete the demo samples and their history, to start over.
cd /d "%~dp0.."
uv run python demo\demo.py reset
pause
