@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo Installez uv avant de lancer NPC : https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
set "NPC_UV_EXTRA="
nvidia-smi --query-gpu=name --format=csv,noheader >nul 2>nul
if not errorlevel 1 set "NPC_UV_EXTRA=--extra cuda"
uv run --python 3.12 --locked --extra pocket %NPC_UV_EXTRA% npc gui "%~dp0CompagnieDesHuitCatastrophes"
if errorlevel 1 pause
