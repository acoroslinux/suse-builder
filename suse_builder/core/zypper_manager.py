import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
from suse_builder.core.chroot_manager import ChrootManager

logger = logging.getLogger("zypper_manager")

class ZypperManagerError(Exception):
    pass

class ZypperManager:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any], toolchain=None):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root
        self.toolchain = toolchain

    def _run_zypper(self, args: List[str], check: bool = False):
        if self.toolchain:
            return self.toolchain.run_tool("zypper", args, check=check)
        return subprocess.run(["zypper", *args], check=check)

    def resolve_cache_dir(self) -> Path:
        arch = getattr(self.chroot, "arch", "x86_64")
        cache_path_str = self.config.get("system", {}).get("zypper_cache", f"cache/{arch}/zypper")
        candidate = Path(cache_path_str)
        if not candidate.is_absolute():
            from suse_builder.core.path_utils import resolve_from_project
            candidate = resolve_from_project(candidate)

        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "suse-builder-cache" / "zypper" / arch
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def add_repositories(self):
        if self.chroot.mode == "mock":
            return

        repos = self.config.get("repos", [])
        for r in repos:
            name = r.get("name", "repo")
            url = r.get("url")
            repo_file = self.target_root / "etc" / "zypp" / "repos.d" / f"{name}.repo"
            if url and not repo_file.exists():
                self._run_zypper(["--root", str(self.target_root), "ar", "-f", url, name])

    def bootstrap_rootfs(self, distro: str, arch: str, use_seed: bool = True):
        if self.chroot.mode == "mock":
            try:
                self.target_root.mkdir(parents=True, exist_ok=True)
                for d in ["etc/zypp", "boot", "usr/bin", "var/cache/zypp"]:
                    (self.target_root / d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.debug("Mock rootfs creation ignored due to permissions.")
            return

        seed_cache = self.resolve_cache_dir().parent / f"seed-{distro}-{arch}.tar.gz"
        seed_used = False

        if use_seed and seed_cache.exists():
            logger.info(f"⚡ Fast-bootstrapping rootfs from local seed tarball: {seed_cache}")
            self.target_root.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(["tar", "xzpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"])
            if res.returncode == 0:
                logger.info("Successfully bootstrapped rootfs from local seed tarball in seconds!")
                seed_used = True
            else:
                logger.warning("Local seed tarball extraction failed. Falling back to Zypper bootstrap.")

        if not seed_used:
            self.add_repositories()
            cmd = [
                "zypper", "--non-interactive", "--root", str(self.target_root),
                "--no-gpg-checks", "install", "--force-resolution", "-y", "--type", "pattern", "base"
            ]
            res = self._run_zypper(cmd[1:])
            if res.returncode != 0:
                raise ZypperManagerError(f"Zypper base pattern installation failed with exit code {res.returncode}")

        # The appliance/base pattern is intentionally tiny.  RPM scriptlets
        # from desktop and hardware packages require these POSIX utilities,
        # so install them before resolving user-selected profiles.
        bootstrap_tools = ["bash", "coreutils", "findutils", "grep", "sed", "shadow"]
        tool_cmd = [
            "zypper", "--non-interactive", "--root", str(self.target_root),
            "--no-gpg-checks", "install", "--force-resolution", "-y", *bootstrap_tools,
        ]
        res = self._run_zypper(tool_cmd[1:])
        if res.returncode != 0:
            raise ZypperManagerError(f"Could not install rootfs bootstrap utilities (exit code {res.returncode})")

        if not seed_used:
            try:
                logger.info(f"⚡ Fast-caching rootfs seed tarball to {seed_cache}...")
                cache_result = subprocess.run([
                    "tar", "czpf", str(seed_cache),
                    "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*", "--exclude=./tmp/*", "--exclude=./run/*",
                    "-C", str(self.target_root), "."
                ], check=False)
                if cache_result.returncode != 0:
                    logger.warning("Could not save seed tarball cache.")
            except Exception as e:
                logger.warning(f"Could not save seed tarball cache: {e}")

    def refresh(self):
        if self.chroot.mode == "mock":
            return
        result = self._run_zypper(["--non-interactive", "--no-gpg-checks", "--root", str(self.target_root), "refresh"])
        if result.returncode != 0:
            raise ZypperManagerError(f"Zypper refresh failed with exit code {result.returncode}")

    def install_packages(self, packages: List[str]):
        if not packages or self.chroot.mode == "mock":
            return
        real_pkgs = [p for p in packages if p]
        if not real_pkgs:
            return
        cmd = ["zypper", "--non-interactive", "--root", str(self.target_root), "--no-gpg-checks", "install", "--force-resolution", "-y"] + real_pkgs
        res = self._run_zypper(cmd[1:])
        if res.returncode != 0:
            raise ZypperManagerError(f"Zypper package installation failed with exit code {res.returncode}")

    def clean_cache(self):
        if self.chroot.mode == "mock":
            return
        subprocess.run(["zypper", "--root", str(self.target_root), "clean", "--all"], check=False)
