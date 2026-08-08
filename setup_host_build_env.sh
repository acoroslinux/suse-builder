#!/bin/bash
# SUSE-Builder: Host Build Environment Setup
set -e

if [ "$EUID" -ne 0 ]; then 
    echo "Please run with sudo."
    exit 1
fi

echo "Detecting package manager..."
if command -v zypper >/dev/null 2>&1; then
    echo "Installing via zypper..."
    zypper in -y zypper xorriso squashfs mtools dosfstools qemu-linux-user
elif command -v dnf >/dev/null 2>&1; then
    echo "Installing via dnf..."
    dnf install -y xorriso squashfs-tools mtools dosfstools qemu-user-static
elif command -v apt >/dev/null 2>&1; then
    echo "Installing via apt..."
    apt update
    apt install -y xorriso squashfs-tools mtools dosfstools qemu-user-static
elif command -v pacman >/dev/null 2>&1; then
    echo "Installing via pacman..."
    pacman -Sy --noconfirm xorriso squashfs-tools mtools dosfstools qemu-user-static-binfmt
elif command -v emerge >/dev/null 2>&1; then
    echo "Installing via emerge (Gentoo)..."
    emerge -uN dev-libs/libisoburn sys-fs/squashfs-tools sys-fs/mtools sys-boot/grub
else
    echo "Unsupported package manager. Please install xorriso, squashfs-tools, mtools manually."
    exit 1
fi

echo "Host build environment setup complete!"
