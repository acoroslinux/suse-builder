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
    zypper in -y zypper xorriso squashfs mtools dosfstools qemu-linux-user grub2-x86_64-efi grub2-i386-efi grub2-i386-pc
elif command -v dnf >/dev/null 2>&1; then
    echo "Installing via dnf..."
    dnf install -y xorriso squashfs-tools mtools dosfstools qemu-user-static grub2-tools grub2-efi-x64-modules grub2-efi-ia32-modules grub2-pc-modules
elif command -v apt >/dev/null 2>&1; then
    echo "Installing via apt..."
    apt update
    apt install -y xorriso squashfs-tools mtools dosfstools qemu-user-static grub-common grub-efi-amd64-bin grub-efi-ia32-bin grub-pc-bin
elif command -v pacman >/dev/null 2>&1; then
    echo "Installing via pacman..."
    pacman -Sy --noconfirm xorriso squashfs-tools mtools dosfstools qemu-user-static-binfmt grub
elif command -v emerge >/dev/null 2>&1; then
    echo "Installing via emerge (Gentoo)..."
    GRUB_PLATFORMS="efi-64 efi-32 pc" emerge -uN dev-libs/libisoburn sys-fs/squashfs-tools sys-fs/mtools sys-boot/grub
else
    echo "Unsupported package manager. Please install xorriso, squashfs-tools, mtools manually."
    exit 1
fi

echo "Host build environment setup complete!"
