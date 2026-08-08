# SUSE-Builder

**Modular and Dynamic openSUSE Linux ISO & Image Builder**

`suse-builder` is a Python-based build system for creating customized openSUSE Linux live ISOs, raw disk images, rootfs tarballs, and OCI container images. It follows the same modular, profile-driven architecture as its sibling builders (`gentoo-builder`, `fedora-builder`, `deb-dev-builder`, `arch-builder`, `void-builder`) while embracing openSUSE ecosystems (`zypper`, `libzypp`, `patterns`, `Packman codecs`, `systemd`, `dracut`).

---

## Features

- 🎯 **Profile-Driven**: JSON profiles for distros, desktops, packages, services, repos, variants, kernels, and bootloaders
- 🦎 **openSUSE Native**: Direct bootstrap via `zypper --installroot`
- 🌟 **Multi-Distro**: openSUSE Tumbleweed (Rolling), Leap 15.6 (Stable), Leap 16.0 (Next), Slowroll
- 🏛️ **Multi-Architecture**: `x86_64`, `i586`, `aarch64`, `riscv64`
- 🍿 **Packman Integration**: Automatic Packman repository setup for H.264/AAC/VLC/FFmpeg multimedia codecs
- ⚡ **Seed Tarball Caching**: Instant rootfs bootstrap via local seed tarball caching (`stage3_seeds`)
- 🔒 **Secure Boot & Quad-Boot**: `shim` + GRUB2 EFI (`BOOTX64.EFI` + `BOOTIA32.EFI`) + ISOLINUX BIOS
- 📦 **Flathub & ZRAM Ready**: Automatic systemd-zram-generator and Flathub flatpak integration
- 🎨 **Desktop Environments**: KDE Plasma, GNOME, XFCE, MATE, LXQt, Sway, Hyprland
- 💿 **Calamares Installer**: Integrated GUI installer launcher
- 🔍 **Mock Mode**: Full build simulation without root privileges

---

## Quick Start

```bash
# Simulate an openSUSE Tumbleweed KDE Plasma ISO build (no root required)
python cli.py x86_64 --distro tumbleweed --desktop kde --mode mock

# Build a real openSUSE Leap 15.6 XFCE ISO with Packman multimedia codecs (requires root)
sudo python cli.py x86_64 --distro leap-15.6 --desktop xfce --multimedia-codecs --mode real

# Build openSUSE Tumbleweed with Calamares and ZRAM
sudo python cli.py x86_64 --distro tumbleweed --desktop gnome --with-calamares --with-zram --mode real

# List all available profiles
python cli.py --list-options

# Validate configuration without building
python cli.py x86_64 --distro tumbleweed --validate
```