@echo off
REM Builds dist\VoiceForeverAgent.exe (a window app) with Python 3.15.
REM Needs uv: https://docs.astral.sh/uv/  (winget install astral-sh.uv)
REM uv downloads Python 3.15 and the exact library versions from uv.lock by itself.
REM SDL3.dll (gamepad and microphone) is downloaded from libsdl-org/SDL releases once
REM and checked against the same SHA-256 as in .github\workflows\agent-exe.yml.
set SDL_VERSION=3.4.8
set SDL_SHA256=bba56c4fd0b2582928605614c620d217a2f1f98eb118df8b1c768af4b7159572
if not exist SDL3.dll (
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $zip=Join-Path $env:TEMP 'SDL3.zip'; Invoke-WebRequest ('https://github.com/libsdl-org/SDL/releases/download/release-%SDL_VERSION%/SDL3-%SDL_VERSION%-win32-x64.zip') -OutFile $zip; if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower() -ne '%SDL_SHA256%') { throw 'SDL3 zip hash mismatch' }; $dir=Join-Path $env:TEMP 'vf-sdl3'; Expand-Archive $zip -DestinationPath $dir -Force; Copy-Item (Join-Path $dir 'SDL3.dll') SDL3.dll" || goto :failed
)
uv sync --locked || goto :failed
uv run pyinstaller --noconfirm --onefile --windowed --name VoiceForeverAgent --icon vf_agent\gui\VoiceForever.ico --add-data "vf_agent\gui\VoiceForever.ico;vf_agent\gui" --add-data "vf_agent\ui;vf_agent\ui" --add-binary "SDL3.dll;." run_agent.py || goto :failed
echo.
echo Done: dist\VoiceForeverAgent.exe. Start it: the window walks you through the settings.
pause
exit /b 0

:failed
echo.
echo Build failed, see the messages above.
pause
exit /b 1
