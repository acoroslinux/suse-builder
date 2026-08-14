import json
from pathlib import Path

b_hybrid = Path('configs/bootloaders/grub2-hybrid.json')
b_uefi = Path('configs/bootloaders/grub2-uefi.json')
d_kde = Path('configs/desktops/kde.json')
p_base = Path('configs/packages/base.json')
p_sys = Path('configs/packages/system-utils.json')
p_xorg = Path('configs/packages/xorg.json')

b_hybrid.write_text(json.dumps({
  "packages": ["grub2", "grub2-x86_64-efi", "grub2-i386-efi", "shim"],
  "bootloader": {"type": "grub2-hybrid"}
}, indent=2))

b_uefi.write_text(json.dumps({
  "packages": ["grub2-x86_64-efi", "grub2-i386-efi", "shim"],
  "bootloader": {"type": "grub2-uefi"}
}, indent=2))

d_kde_data = json.loads(d_kde.read_text())
new_kde_pkgs = ["plasma5-desktop", "plasma-workspace", "sddm", "xorg-x11-server", "xf86-video-vmware", "xf86-input-evdev", "dbus-1-x11", "xterm"]
for p in new_kde_pkgs:
    if p not in d_kde_data["packages"]:
        d_kde_data["packages"].append(p)
d_kde.write_text(json.dumps(d_kde_data, indent=2))

p_base_data = json.loads(p_base.read_text())
new_base = ["live-boot", "dracut", "bash", "coreutils", "iproute2", "iputils", "curl", "wget", "zstd", "glibc-locale", "ca-certificates", "ca-certificates-mozilla", "kernel-firmware-all"]
for p in new_base:
    if p not in p_base_data["packages"]:
        p_base_data["packages"].append(p)
p_base.write_text(json.dumps(p_base_data, indent=2))

p_sys_data = json.loads(p_sys.read_text())
p_sys_data["packages"] = ["htop", "btop", "fastfetch", "tmux", "zsh", "fzf", "ripgrep", "bat", "unzip", "p7zip", "rsync", "tree", "lsof"]
p_sys.write_text(json.dumps(p_sys_data, indent=2))

p_xorg_data = json.loads(p_xorg.read_text())
new_xorg = ["xorg-x11-server", "xf86-video-all", "xf86-video-vmware", "xf86-input-evdev", "xf86-input-libinput", "x11-tools", "dbus-1-x11"]
for p in new_xorg:
    if p not in p_xorg_data["packages"]:
        p_xorg_data["packages"].append(p)
p_xorg.write_text(json.dumps(p_xorg_data, indent=2))
