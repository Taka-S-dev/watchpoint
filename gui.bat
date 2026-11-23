@echo off
rem Open watchpoint in its own window (no browser, no server). Closing the window ends it.
rem Extra arguments are passed on, e.g.  gui.bat -c D:\work\targets.yaml
setlocal
cd /d "%~dp0"
where uv >NUL 2>NUL
if errorlevel 1 (
  echo uv was not found. Install it from https://docs.astral.sh/uv/ and run this again.
  pause
  exit /b 1
)
uv sync --quiet
if errorlevel 1 (
  echo Could not prepare the Python environment. See the message above.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m watchpoint gui %*
