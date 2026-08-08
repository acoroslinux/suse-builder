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

    def resolve_cache_dir(self) -> Path:
        arch = getattr(self.chroot, "arch", "x86_64")
        cache_path_str = self.config.get("system", {}).get("zypper_cache", "workdir/cache/zypper")
        candidate = Path(cache_path_str)
        if not candidate.is_absolute():
            from suse_builder.core.path_utils import resolve_from_project
            candidate = resolve_from_project(candidate) / arch

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
            if url:
                cmd = ["zypper", "--root", str(self.target_root), "ar", "-f", url, name]
                subprocess.run(cmd, check=False)

    def bootstrap_rootfs(self, distro: str, arch: str, use_seed: bool = True):
        if self.chroot.mode == "mock":
            try:
                self.target_root.mkdir(parents=True, exist_ok=True)
                for d in ["etc/zypp", "boot", "usr/bin", "var/cache/zypp"]:
                    (self.target_root / d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.debug("Mock rootfs creation ignored due to permissions.")
            return

        seed_cache = self.resolve_cache_dir() / f"seed-{distro}-{arch}.tar.xz"

        if use_seed and seed_cache.exists():
            logger.info(f"⚡ Fast-bootstrapping rootfs from local seed tarball: {seed_cache}")
            self.target_root.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(["tar", "xpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"])
            if res.returncode == 0:
                logger.info("Successfully bootstrapped rootfs from local seed tarball in seconds!")
                return
            else:
                logger.warning("Local seed tarball extraction failed. Falling back to Zypper bootstrap.")

        self.add_repositories()
        cmd = [
            "zypper", "--non-interactive", "--root", str(self.target_root),
            "--no-gpg-checks", "install", "-y", "--type", "pattern", "base"
        ]
        res = subprocess.run(cmd)
        if res.returncode != 0:
            logger.warning(f"Zypper base pattern installation returned exit code {res.returncode}")

        try:
            logger.info(f"Caching rootfs seed tarball to {seed_cache}...")
            subprocess.run(["tar", "cJpf", str(seed_cache), "-C", str(self.target_root), "."], check=False)
        except Exception as e:
            logger.warning(f"Could not save seed tarball cache: {e}")

    def refresh(self):
        if self.chroot.mode == "mock":
            return
        subprocess.run(["zypper", "--non-interactive", "--root", str(self.target_root), "refresh"], check=False)

    def install_packages(self, packages: List[str]):
        if not packages or self.chroot.mode == "mock":
            return
        real_pkgs = [p for p in packages if p]
        if not real_pkgs:
            return
        cmd = ["zypper", "--non-interactive", "--root", str(self.target_root), "--no-gpg-checks", "install", "-y"] + real_pkgs
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            logger.warning(f"Zypper package installation returned code {res.returncode}")

    def clean_cache(self):
        if self.chroot.mode == "mock":
            return
        subprocess.run(["zypper", "--root", str(self.target_root), "clean", "--all"], check=False)
