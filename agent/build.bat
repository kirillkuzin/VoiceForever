@echo off
REM Builds dist\VoiceForeverAgent.exe with Python 3.15.
REM Needs uv: https://docs.astral.sh/uv/  (winget install astral-sh.uv)
REM uv downloads Python 3.15 and the exact library versions from uv.lock by itself.
uv sync --locked
uv run pyinstaller --onefile --console --name VoiceForeverAgent run_agent.py
echo.
echo Done: dist\VoiceForeverAgent.exe. Start it once to create config.toml next to it.
pause
