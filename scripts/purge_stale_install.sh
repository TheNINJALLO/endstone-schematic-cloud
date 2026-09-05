#!/usr/bin/env sh
set -eu

PLUGIN_DIR="${1:-/home/container/plugins}"

echo "Purging stale Ninj-OS Schematics wheels from: $PLUGIN_DIR"
find "$PLUGIN_DIR" -maxdepth 1 -type f -name 'endstone_ninjos_schematics-*.whl' -print -delete 2>/dev/null || true

for SITE in "$PLUGIN_DIR"/.local/lib/python*/site-packages; do
    [ -d "$SITE" ] || continue
    echo "Purging cached package from: $SITE"
    rm -rf "$SITE/endstone_ninjos_schematics"
    rm -rf "$SITE"/endstone_ninjos_schematics-*.dist-info
    rm -rf "$SITE"/endstone_ninjos_schematics-*.egg-info
    find "$SITE" -maxdepth 1 -type d -name 'endstone_ninjos_schematics-*.dist-info' -exec rm -rf {} + 2>/dev/null || true
done

echo "Purge complete. Upload only endstone_ninjos_schematics-1.6.1-py3-none-any.whl, then start the server."
