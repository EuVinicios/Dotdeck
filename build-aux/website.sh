#!/usr/bin/env bash
# Assemble the GitHub Pages site. The icon, the pets and the screenshots are copied
# from data/ and src/pets, so the site always shows what the app ships.
#
#   build-aux/website.sh [OUTPUT_DIR]      (default: build/website)
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="${1:-build/website}"
ICONS=data/icons/hicolor
APP_ID=io.github.euvinicios.Dotdeck

rm -rf "$OUT"
mkdir -p "$OUT/assets/pets" "$OUT/screenshots"
cp -R website/. "$OUT/"
for size in 64 256 512; do
    cp "$ICONS/${size}x${size}/apps/$APP_ID.png" "$OUT/assets/icon-$size.png"
done
cp src/pets/*.png "$OUT/assets/pets/"
# The site speaks Brazilian Portuguese, and so do its screenshots (light and dark).
cp data/screenshots/pt_BR/*.png "$OUT/screenshots/"
cp data/store/social-card.png "$OUT/assets/social-card.png"
touch "$OUT/.nojekyll"
echo "Site gerado em $OUT"
