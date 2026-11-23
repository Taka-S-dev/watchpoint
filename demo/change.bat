@echo off
rem Make the next round of changes to the demo samples, then press "check all" in the window.
cd /d "%~dp0.."
uv run python demo\demo.py change
pause
