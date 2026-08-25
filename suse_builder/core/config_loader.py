import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from suse_builder.core.path_utils import resolve_from_project

class ConfigLoaderError(Exception):
    """Exception raised for configuration loading errors."""
    pass

class ConfigLoader:
    def __init__(self, config_root: Optional[Path] = None):
        self.config_root = config_root or resolve_from_project("configs")

    def load_json(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise ConfigLoaderError(f"Configuration file not found: {path}")
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigLoaderError(f"Invalid JSON in {path}: {e}")
        except Exception as e:
            raise ConfigLoaderError(f"Error loading {path}: {e}")

    def load_profile(self, category: str, profile_name: str) -> Dict[str, Any]:
        path = self.config_root / category / f"{profile_name}.json"
        return self.load_json(path)

    def _merge_dicts(self, base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
        result = base.copy()
        for key, value in update.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_dicts(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                existing = list(result[key])
                extras = [item for item in value if item not in existing]
                result[key] = existing + extras
            else:
                result[key] = value
        return result

    def assemble_build_config(
        self,
        global_config_path: Path,
        architecture: str,
        distro: str,
        desktop: Optional[str] = None,
        kernel: Optional[str] = None,
        bootloader: Optional[str] = None,
        variant: Optional[str] = None,
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
        device: Optional[str] = None,
    ) -> Dict[str, Any]:

        config = {
            "packages": [],
            "groups": [],
            "services": {"enable": [], "disable": []},
            "repos": [],
            "kernel_packages": [],
            "bootloader": {},
            "live_user": {},
            "distro_info": {},
            "arch_info": {},
            "system": {},
            "boot": {},
            "variant_info": {},
            "device_info": {}
        }

        # 1. Global config
        config = self._merge_dicts(config, self.load_json(global_config_path))

        # 2. Distro
        if distro:
            distro_name = distro
            if distro_name == "leap" and not (self.config_root / "distros" / "leap.json").exists():
                distro_name = "leap-16.0" if (self.config_root / "distros" / "leap-16.0.json").exists() else "leap-15.6"
            config = self._merge_dicts(config, self.load_profile("distros", distro_name))

        # 3. Architecture
        config = self._merge_dicts(config, self.load_profile("architectures", architecture))

        # 4. Variant
        if variant:
            config = self._merge_dicts(config, self.load_profile("variants", variant))

        # 5. Device / Board Profile
        if device:
            config = self._merge_dicts(config, self.load_profile("devices", device))

        # 6. Desktop
        if desktop:
            config = self._merge_dicts(config, self.load_profile("desktops", desktop))
            # Automatically include xorg package profile for graphical display server support
            if desktop not in {"minimal", "server", "cloud"}:
                config = self._merge_dicts(config, self.load_profile("packages", "xorg"))
            # Automatically include mobile-base for mobile desktop environments
            if desktop in {"plasma-mobile", "phosh", "sxmo"}:
                config = self._merge_dicts(config, self.load_profile("packages", "mobile-base"))

        # 7. Kernel
        if kernel:
            config = self._merge_dicts(config, self.load_profile("kernels", kernel))

        # 8. Bootloader
        if bootloader:
            config = self._merge_dicts(config, self.load_profile("bootloaders", bootloader))

        # 9. Base packages
        config = self._merge_dicts(config, self.load_profile("packages", "base"))

        # 10. Package profiles
        if package_profiles:
            for profile in package_profiles:
                config = self._merge_dicts(config, self.load_profile("packages", profile))

        # 10. Service profiles
        if service_profiles:
            for profile in service_profiles:
                config = self._merge_dicts(config, self.load_profile("services", profile))

        # 11. Repo profiles
        if repo_profiles:
            for profile in repo_profiles:
                config = self._merge_dicts(config, self.load_profile("repos", profile))

        # 12. Live User profile
        if live_profile:
            config["live_user"] = self._merge_dicts(config.get("live_user", {}), self.load_profile("live-users", live_profile))

        # 13. Deduplicate lists
        for key in ["packages", "groups", "kernel_packages"]:
            if key in config and isinstance(config[key], list):
                config[key] = list(dict.fromkeys(config[key]))

        if "repos" in config and isinstance(config["repos"], list):
            unique_repos = []
            seen_urls = set()
            for r in config["repos"]:
                if isinstance(r, dict):
                    url = r.get("url")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        unique_repos.append(r)
                elif r not in unique_repos:
                    unique_repos.append(r)
        # 14. Architecture-aware Repository URL transformation
        arch_lower = architecture.lower()
        if arch_lower in {"aarch64", "arm64", "armv7l", "armv7hl", "riscv64"}:
            port_name = "aarch64" if arch_lower in {"aarch64", "arm64"} else ("riscv" if arch_lower == "riscv64" else "armv7hl")
            adapted_repos = []
            for r in config.get("repos", []):
                if isinstance(r, dict):
                    name = str(r.get("name", "")).lower()
                    url = str(r.get("url", "")).lower()
                    # openSUSE ports do not maintain non-oss repositories
                    if "non-oss" in name or "/non-oss" in url:
                        continue
                    r_copy = dict(r)
                    url_orig = r_copy.get("url", "")
                    if "download.opensuse.org/tumbleweed/" in url_orig:
                        r_copy["url"] = url_orig.replace("download.opensuse.org/tumbleweed/", f"download.opensuse.org/ports/{port_name}/tumbleweed/")
                    elif "download.opensuse.org/distribution/" in url_orig:
                        r_copy["url"] = url_orig.replace("download.opensuse.org/distribution/", f"download.opensuse.org/ports/{port_name}/distribution/")
                    elif "download.opensuse.org/update/" in url_orig:
                        r_copy["url"] = url_orig.replace("download.opensuse.org/update/", f"download.opensuse.org/ports/{port_name}/update/")
                    elif "download.opensuse.org/slowroll/" in url_orig:
                        r_copy["url"] = url_orig.replace("download.opensuse.org/slowroll/", f"download.opensuse.org/ports/{port_name}/slowroll/")
                    adapted_repos.append(r_copy)
                else:
                    adapted_repos.append(r)
            config["repos"] = adapted_repos

        # 15. Intelligent Distro-Aware & Arch-Aware Package Normalization
        distro_str = str(config.get("distro", "")).lower()
        is_tumbleweed_or_rolling = any(k in distro_str for k in ["tumbleweed", "slowroll", "leap-16", "factory"])

        package_mappings_tumbleweed = {
            "plasma5-workspace": "plasma6-workspace",
            "plasma5-desktop": "plasma6-desktop",
            "plasma5-workspace-libs": "plasma6-workspace-libs",
            "plasma-mobile": "plasma6-mobile",
            "plasma-nm": "plasma6-nm",
            "plasma-pa": "plasma6-pa",
            "plasma-phone-components": "plasma6-mobile",
            "mesa-dri-nouveau": "Mesa-dri-nouveau",
            "mesa-dri-gallium": "Mesa-dri",
            "mesa-dri-panfrost": "Mesa-dri",
            "bluez-tools": "bluez",
            "NetworkManager-wifi": "NetworkManager",
            "dtb-allwinner": "kernel-default",
            "dtb-rockchip": "kernel-default",
            "dtb-qualcomm": "kernel-default",
            "anx7688-firmware": "kernel-firmware-all",
            "rtl8723bt-firmware": "kernel-firmware-all",
            "bes2600-firmware": "kernel-firmware-all",
            "eg25-manager": "ModemManager",
            "spacebar": "plasma-phonebook",
            "vvave": "plasma-camera",
        }

        package_mappings_leap = {
            "plasma6-workspace": "plasma5-workspace",
            "plasma6-desktop": "plasma5-desktop",
            "plasma6-mobile": "plasma5-workspace",
            "plasma6-nm": "plasma-nm",
            "plasma6-pa": "plasma-pa",
            "mesa-dri-nouveau": "Mesa-dri-nouveau",
            "mesa-dri-gallium": "Mesa-dri",
            "mesa-dri-panfrost": "Mesa-dri",
            "bluez-tools": "bluez",
            "NetworkManager-wifi": "NetworkManager",
            "dtb-allwinner": "kernel-default",
            "dtb-rockchip": "kernel-default",
            "dtb-qualcomm": "kernel-default",
            "anx7688-firmware": "kernel-firmware-all",
            "rtl8723bt-firmware": "kernel-firmware-all",
            "bes2600-firmware": "kernel-firmware-all",
            "eg25-manager": "ModemManager",
            "plasma-mobile": "plasma5-workspace",
            "plasma-phone-components": "plasma5-workspace",
        }

        current_map = package_mappings_tumbleweed if is_tumbleweed_or_rolling else package_mappings_leap

        translated_packages = []
        for pkg in config.get("packages", []):
            mapped = current_map.get(pkg, pkg)
            if mapped and mapped not in translated_packages:
                translated_packages.append(mapped)

        x86_only_packages = {
            "ucode-intel",
            "ucode-amd",
            "xf86-video-intel",
            "xf86-video-vmware",
            "xf86-video-vesa",
            "xf86-video-qxl",
            "virtualbox-guest-tools",
            "grub2-i386-efi",
            "grub2-x86_64-efi",
            "syslinux",
            "libvulkan_intel",
        }
        if arch_lower in {"aarch64", "arm64", "armv7l", "armv7hl", "riscv64"}:
            translated_packages = [p for p in translated_packages if p not in x86_only_packages]

        config["packages"] = translated_packages

        return config
