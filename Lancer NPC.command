#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    echo "Installez uv : https://docs.astral.sh/uv/getting-started/installation/"
    read -r '?Appuyez sur Entrée pour fermer…'
    exit 1
fi
uv run --python 3.12 --locked --extra pocket --extra qwen npc gui
if [[ $? -ne 0 ]]; then
    read -r '?Appuyez sur Entrée pour fermer…'
fi
