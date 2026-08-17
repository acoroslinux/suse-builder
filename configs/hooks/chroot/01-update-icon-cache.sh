#!/bin/bash
# Hook: Rebuild GTK Icon Caches
# Rebuilding icon caches drastically speeds up application startup
# and reduces memory footprint, especially in a Live environment.

echo "Rebuilding GTK Icon Caches..."

# Find all icon theme directories that have an index.theme file
for d in /usr/share/icons/*; do
    if [ -d "$d" ] && [ -f "$d/index.theme" ]; then
        echo "Updating icon cache for: $(basename "$d")"
        gtk-update-icon-cache -f -q "$d" 2>/dev/null || true
    fi
done

echo "Icon cache rebuilt successfully!"
