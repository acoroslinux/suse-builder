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
            "variant_info": {}
        }

        # 1. Global config
        config = self._merge_dicts(config, self.load_json(global_config_path))

        # 2. Distro
        if distro:
            config = self._merge_dicts(config, self.load_profile("distros", distro))

        # 3. Architecture
        config = self._merge_dicts(config, self.load_profile("architectures", architecture))

        # 4. Variant
        if variant:
            config = self._merge_dicts(config, self.load_profile("variants", variant))

        # 5. Desktop
        if desktop:
            config = self._merge_dicts(config, self.load_profile("desktops", desktop))

        # 6. Kernel
        if kernel:
            config = self._merge_dicts(config, self.load_profile("kernels", kernel))

        # 7. Bootloader
        if bootloader:
            config = self._merge_dicts(config, self.load_profile("bootloaders", bootloader))

        # 8. Base packages
        config = self._merge_dicts(config, self.load_profile("packages", "base"))

        # 9. Package profiles
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
            config["repos"] = unique_repos

        if "services" in config:
            for state in ["enable", "disable"]:
                if state in config["services"] and isinstance(config["services"][state], list):
                    config["services"][state] = list(dict.fromkeys(config["services"][state]))

        return config
