@echo off
REM Builds dist\VoiceForeverAgent.exe (a window app) with Python 3.15.
REM Needs uv: https://docs.astral.sh/uv/  (winget install astral-sh.uv)
REM uv downloads Python 3.15 and the exact library versions from uv.lock by itself.
uv sync --locked || goto :failed
uv run python -m vf_agent.gui.icons build\VoiceForever.ico || goto :failed
uv run pyinstaller --noconfirm --onefile --windowed --name VoiceForeverAgent --icon build\VoiceForever.ico --add-data "vf_agent\ui;vf_agent\ui" run_agent.py || goto :failed
echo.
echo Done: dist\VoiceForeverAgent.exe. Start it: the window walks you through the settings.
pause
exit /b 0

:failed
echo.
echo Build failed, see the messages above.
pause
exit /b 1
