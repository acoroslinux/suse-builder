#!/bin/sh
# Script to dynamically create the "Install System" shortcut on the live user desktop

# Exit immediately if we are NOT in a LiveCD environment
if ! grep -q -E "(rd\.live|root=live|boot=live|livecd)" /proc/cmdline 2>/dev/null; then
    exit 0
fi

# Wait a few seconds for the user session and xdg directories to initialize
sleep 2

# Query the localized Desktop folder name (e.g. ~/Ambiente de Trabalho)
DESKTOP=$(xdg-user-dir DESKTOP 2>/dev/null)
if [ -z "$DESKTOP" ]; then
    DESKTOP="$HOME/Desktop"
fi

# Ensure the directory exists
mkdir -p "$DESKTOP"

LAUNCHER_DEST="$DESKTOP/install.desktop"

# Create the desktop file dynamically
cat << 'EOF' > "$LAUNCHER_DEST"
[Desktop Entry]
Type=Application
Version=1.0
Name=Install Gentoo Modern
GenericName=System Installer
Comment=Install Gentoo Modern operating system to disk
Exec=sudo -E calamares
Icon=calamares
Terminal=false
StartupNotify=true
Categories=System;Qt;
EOF

chmod +x "$LAUNCHER_DEST"

# Set launcher as trusted under XFCE/Gnome to avoid execution warnings
if command -v gio >/dev/null 2>&1; then
    gio set --type=string "$LAUNCHER_DEST" metadata::trusted true 2>/dev/null
    gio set --type=string "$LAUNCHER_DEST" metadata::xfce-exe-checksum "$(sha256sum "$LAUNCHER_DEST" | cut -f1 -d' ')" 2>/dev/null
fi
touch "$LAUNCHER_DEST"
