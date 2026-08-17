#!/bin/bash
# Hook: Rebuild Fontconfig Cache
# Speeds up font rendering in the Live session by avoiding dynamic cache generation.

echo "Rebuilding Fontconfig Caches..."

# Rebuild caches forcefully and system-wide
fc-cache -f -s -v >/dev/null 2>&1

echo "Fontconfig cache rebuilt successfully!"
