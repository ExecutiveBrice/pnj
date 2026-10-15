@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo Installez uv avant de lancer NPC : https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
uv run --python 3.12 --frozen npc gui
if errorlevel 1 pause
