#!/usr/bin/env bash
# Build the mint-radio .deb (no root needed to build, only to install).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PKG="$HERE/deb"
DIST="$REPO/dist"

VER="$(python3 -c "import re; print(re.search(r'VERSION *= *\"([^\"]+)\"', open('$REPO/main.py').read()).group(1))")"
echo "Building mint-radio $VER"

# sync version + app files into the package tree
sed -i "s/^Version: .*/Version: $VER/" "$PKG/DEBIAN/control"
mkdir -p "$PKG/usr/share/mint-radio/assets" "$DIST"
cp "$REPO/main.py" "$REPO/stations.json" "$PKG/usr/share/mint-radio/"
cp "$REPO/assets/icon.png" "$REPO/assets/icon.svg" "$PKG/usr/share/mint-radio/assets/"
cp "$REPO/assets/icon.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/mint-radio.svg" 2>/dev/null || {
  mkdir -p "$PKG/usr/share/icons/hicolor/scalable/apps"
  cp "$REPO/assets/icon.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/mint-radio.svg"
}

# permissions
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/usr/bin/mint-radio"
chmod 644 "$PKG/usr/share/applications/mint-radio.desktop" \
           "$PKG/usr/share/mint-radio/main.py" \
           "$PKG/usr/share/mint-radio/stations.json" \
           "$PKG/usr/share/mint-radio/assets/"* \
           "$PKG/usr/share/icons/hicolor/scalable/apps/mint-radio.svg" \
           "$PKG/usr/share/doc/mint-radio/copyright" \
           "$PKG/DEBIAN/control"
find "$PKG" -type d -exec chmod 755 {} +

DEB="$DIST/mint-radio_${VER}_all.deb"
dpkg-deb --build "$PKG" "$DEB"
echo "Built: $DEB ($(du -h "$DEB" | cut -f1))"
echo "--- contents ---"
dpkg-deb -c "$DEB"
