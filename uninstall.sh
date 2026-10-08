#!/usr/bin/env bash
# Mint Radio uninstaller — removes user-local install only (keeps mpv/system pkgs).
set -euo pipefail
rm -rf "$HOME/.local/share/mint-radio"
rm -f "$HOME/.local/bin/mint-radio"
rm -f "$HOME/.local/share/applications/mint-radio.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/mint-radio.svg"
echo "Mint Radio uninstalled. Config left in ~/.config/mint-radio/ (delete manually if wanted)."
