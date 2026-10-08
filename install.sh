#!/usr/bin/env bash
# Mint Radio installer — Linux Mint 22.x (Cinnamon)
# Installs: mpv, app files to ~/.local/share/mint-radio,
#           launcher to ~/.local/bin/mint-radio,
#           .desktop + icon for the Mint menu.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/mint-radio"
BIN="$HOME/.local/bin/mint-radio"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "== Mint Radio installer =="

# 1. system deps
if ! command -v mpv >/dev/null 2>&1; then
  echo "--> installing mpv + python3-tk (needs sudo)…"
  sudo apt update && sudo apt install -y mpv python3-tk
else
  echo "--> mpv already present: $(mpv --version | head -n1)"
fi
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "--> installing python3-tk (needs sudo)…"
  sudo apt install -y python3-tk
fi

# 2. app files
echo "--> copying app to $DEST"
mkdir -p "$DEST"
cp "$SRC_DIR/main.py" "$SRC_DIR/stations.json" "$DEST/"
mkdir -p "$DEST/assets"
cp "$SRC_DIR/assets/icon.png" "$SRC_DIR/assets/icon.svg" "$DEST/assets/" 2>/dev/null || true
chmod +x "$DEST/main.py"

# 3. launcher wrapper (ensures correct cwd for stations.json)
mkdir -p "$HOME/.local/bin"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec python3 "$DEST/main.py" "\$@"
EOF
chmod +x "$BIN"
echo "--> launcher: $BIN"

# 4. menu entry + icon
mkdir -p "$APPS" "$ICONS"
cp "$SRC_DIR/mint-radio.desktop" "$APPS/"
if [ -f "$SRC_DIR/assets/icon.svg" ]; then
  cp "$SRC_DIR/assets/icon.svg" "$ICONS/mint-radio.svg"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

# 5. sanity check
python3 -m py_compile "$DEST/main.py"
python3 -c "import json; d=json.load(open('$DEST/stations.json')); print(f\"stations: {len(d['stations'])}\")"

echo ""
echo "Done! Launch with:  mint-radio"
echo "…or find 'Mint Radio' in the Mint menu (log out/in if it doesn't appear yet)."
echo "Config lives in ~/.config/mint-radio/ | recordings in ~/Music/MintRadio/"
