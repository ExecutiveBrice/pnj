@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo Installez uv : https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
echo Pocket TTS francais - modele local sur CPU, audio en streaming.
uv run --project experiments/pocket_tts --python 3.12 --locked python experiments/pocket_tts/demo.py %*
if errorlevel 1 pause
