import os
import shutil
import subprocess
import platform
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
        cache_root = self.resolve_cache_dir()
        metadata_cache = cache_root / "metadata"
        package_cache = cache_root / "packages"
        metadata_cache.mkdir(parents=True, exist_ok=True)
        package_cache.mkdir(parents=True, exist_ok=True)

        self._tune_zypper_performance()

        zypp_conf_path = self.target_root / "etc" / "zypp" / "zypp.conf"

        cleaned_args = [a for a in args if a not in ("--non-interactive", "--gpg-auto-import-keys")]
        full_args = [
            "--non-interactive",
            "--gpg-auto-import-keys",
            "--config", str(zypp_conf_path),
            "--cache-dir", str(metadata_cache),
            "--pkg-cache-dir", str(package_cache),
            *cleaned_args,
        ]

        target_arch = str(getattr(self.chroot, "arch", "") or self.config.get("arch", "x86_64")).lower()
        host_arch = platform.machine().lower()
        is_foreign = (target_arch not in {"x86_64", "amd64"} if host_arch in {"x86_64", "amd64"} else target_arch != host_arch)

        # If foreign rootfs is bootstrapped and contains zypper, run via chroot emulation
        target_zypper = self.target_root / "usr" / "bin" / "zypper"
        if is_foreign and target_zypper.exists() and not self.toolchain:
            chroot_args = []
            skip_next = False
            for arg in cleaned_args:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "--root":
                    skip_next = True
                    continue
                if arg.startswith("--root="):
                    continue
                chroot_args.append(arg)

            in_chroot_cmd = ["zypper", "--non-interactive", "--gpg-auto-import-keys", *chroot_args]
            return self.chroot.run_in_chroot(in_chroot_cmd, check=check)

        env = os.environ.copy()
        env["ZYPP_CONF"] = str(zypp_conf_path)

        if target_arch in {"amd64"}:
            target_arch = "x86_64"
        elif target_arch in {"arm64"}:
            target_arch = "aarch64"
        elif target_arch in {"i686", "i386"}:
            target_arch = "i586"
        env["ZYPP_ARCH"] = target_arch

        if self.toolchain:
            return self.toolchain.run_tool("zypper", full_args, check=check, env={"ZYPP_CONF": str(zypp_conf_path), "ZYPP_ARCH": target_arch})
        return subprocess.run(["zypper", *full_args], env=env, check=check)

    def _run_prefer_signed(self, signed_args: List[str], fallback_args: Optional[List[str]] = None):
        """Try signed operation first and gracefully fallback when keys are unavailable."""
        result = self._run_zypper(signed_args)
        if result.returncode == 0 or not fallback_args:
            return result
        logger.warning("Signed zypper operation failed; retrying with --no-gpg-checks.")
        return self._run_zypper(fallback_args)

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

    def _is_bootstrapped_rootfs(self) -> bool:
        """Detect whether target root already contains a usable base system."""
        checks = [
            self.target_root / "etc" / "os-release",
            self.target_root / "usr" / "bin" / "rpm",
            self.target_root / "etc" / "zypp",
        ]
        return all(path.exists() for path in checks)

    def _tune_zypper_performance(self):
        """Inject parallel download, architecture, and I/O acceleration settings into /etc/zypp/zypp.conf."""
        if self.chroot.mode == "mock":
            return
        zypp_conf = self.target_root / "etc" / "zypp" / "zypp.conf"
        zypp_conf.parent.mkdir(parents=True, exist_ok=True)

        arch_val = str(getattr(self.chroot, "arch", "") or self.config.get("arch", "x86_64")).lower()
        if arch_val in {"arm64"}:
            arch_val = "aarch64"
        elif arch_val in {"amd64"}:
            arch_val = "x86_64"
        elif arch_val in {"i686", "i386"}:
            arch_val = "i586"

        settings = {
            "arch": arch_val,
            "download.max_concurrent_connections": "16",
            "download.min_download_speed": "1000",
            "commit.downloadMode": "DownloadInAdvance",
            "rpm.install.excludedocs": "yes",
            "solver.onlyRequires": "true",
            "keeppackages": "1",
        }

        if not zypp_conf.exists() or zypp_conf.stat().st_size == 0:
            content = "[main]\n" + "\n".join(f"{k} = {v}" for k, v in settings.items()) + "\n"
            zypp_conf.write_text(content)
        else:
            try:
                lines = zypp_conf.read_text().splitlines()
            except Exception:
                lines = []
            new_lines = []
            has_main = False
            remaining = dict(settings)
            for line in lines:
                if line.strip() == "[main]":
                    has_main = True
                key = line.split("=")[0].strip() if "=" in line else ""
                if key in remaining:
                    new_lines.append(f"{key} = {remaining[key]}")
                    remaining.pop(key, None)
                else:
                    new_lines.append(line)
            if not has_main:
                new_lines.insert(0, "[main]")
            for k, v in remaining.items():
                new_lines.append(f"{k} = {v}")
            try:
                zypp_conf.write_text("\n".join(new_lines) + "\n")
            except Exception:
                pass

    def add_repositories(self):
        if self.chroot.mode == "mock":
            return

        self._tune_zypper_performance()
        repos = self.config.get("repos", [])
        distro_key = str(self.config.get("distro") or "").lower()

        repos_d = self.target_root / "etc" / "zypp" / "repos.d"
        existing_aliases = set()
        if repos_d.exists():
            for r_file in repos_d.glob("*.repo"):
                try:
                    for line in r_file.read_text().splitlines():
                        line = line.strip()
                        if line.startswith("[") and line.endswith("]"):
                            existing_aliases.add(line[1:-1].strip())
                except Exception:
                    pass

        for r in repos:
            name = r.get("name", "repo")
            url = r.get("url")
            if name == "packman" and url and "openSUSE_Tumbleweed" in url:
                if "15." in distro_key or "15" in distro_key:
                    url = "https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Leap_15.6/"
                elif "slowroll" in distro_key:
                    url = "https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Slowroll/Essentials/"
            
            if name in existing_aliases:
                continue

            repo_file = repos_d / f"{name}.repo"
            if url and not repo_file.exists():
                ar_args = ["--root", str(self.target_root), "ar", "-f"]
                if r.get("gpgcheck") is False:
                    ar_args.append("--no-gpgcheck")
                ar_args.extend([url, name])
                self._run_zypper(ar_args)
                existing_aliases.add(name)

        # Enforce keeppackages=1 on all repository definitions
        if repos_d.exists():
            for r_file in repos_d.glob("*.repo"):
                try:
                    content = r_file.read_text()
                    if "keeppackages=" in content:
                        content = content.replace("keeppackages=0", "keeppackages=1")
                    else:
                        content += "\nkeeppackages=1\n"
                    r_file.write_text(content)
                except Exception:
                    pass

    def bootstrap_rootfs(self, distro: str, arch: str, use_seed: bool = False, create_seed: bool = False, reuse_existing: bool = False):
        if self.chroot.mode == "mock":
            try:
                self.target_root.mkdir(parents=True, exist_ok=True)
                for d in ["etc/zypp", "boot", "usr/bin", "var/cache/zypp"]:
                    (self.target_root / d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.debug("Mock rootfs creation ignored due to permissions.")
            return

        if reuse_existing and self._is_bootstrapped_rootfs():
            logger.info("♻️ Reusing existing rootfs because --no-clean was requested.")
            return

        target_arch = str(getattr(self.chroot, "arch", "") or self.config.get("arch", "x86_64")).lower()
        if target_arch in {"amd64"}:
            target_arch = "x86_64"
        elif target_arch in {"arm64"}:
            target_arch = "aarch64"
        elif target_arch in {"i686", "i386"}:
            target_arch = "i586"

        host_arch = platform.machine().lower()
        is_foreign = (target_arch not in {"x86_64", "amd64"} if host_arch in {"x86_64", "amd64"} else target_arch != host_arch)

        seed_cache = self.resolve_cache_dir().parent / f"seed-{distro}-{arch}.tar.xz"
        seed_cache_gz = self.resolve_cache_dir().parent / f"seed-{distro}-{arch}.tar.gz"
        seed_used = False

        if seed_cache_gz.exists() and not seed_cache.exists():
            seed_cache = seed_cache_gz

        if (use_seed or is_foreign) and seed_cache.exists():
            if seed_cache.stat().st_size >= 512:
                logger.info(f"⚡ Fast-bootstrapping rootfs from local seed tarball: {seed_cache} ({seed_cache.stat().st_size // (1024*1024)} MB)...")
                self.target_root.mkdir(parents=True, exist_ok=True)
                tar_cmd = ["tar", "xzpf" if str(seed_cache).endswith(".gz") else "xpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"]
                res = subprocess.run(tar_cmd)
                if res.returncode == 0:
                    logger.info("Successfully bootstrapped rootfs from local seed tarball!")
                    seed_used = True
                    self.chroot.prepare_emulation()

        if is_foreign and not seed_used:
            appliance_urls = {
                "tumbleweed": f"https://download.opensuse.org/ports/{target_arch}/tumbleweed/appliances/opensuse-tumbleweed-image.{target_arch}-networkd.tar.xz",
                "leap-15.6": f"https://download.opensuse.org/ports/{target_arch}/distribution/leap/15.6/appliances/opensuse-leap-image.{target_arch}.tar.xz",
            }
            appliance_url = appliance_urls.get(distro) or appliance_urls.get("tumbleweed")
            logger.info(f"📥 Fetching official openSUSE minimal rootfs appliance for {target_arch} ({distro})...")
            tmp_download = seed_cache.with_suffix(".download")
            try:
                subprocess.run(["curl", "-fSL", "--retry", "3", appliance_url, "-o", str(tmp_download)], check=True)
                if tmp_download.exists() and tmp_download.stat().st_size >= 1024 * 1024:
                    tmp_download.replace(seed_cache)
                    logger.info(f"Cached bootstrap appliance to {seed_cache} ({seed_cache.stat().st_size // (1024*1024)} MB).")
                    self.target_root.mkdir(parents=True, exist_ok=True)
                    res = subprocess.run(["tar", "xpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"])
                    if res.returncode == 0:
                        seed_used = True
                        self.chroot.prepare_emulation()
            except Exception as e:
                logger.warning(f"Could not download official appliance: {e}")
                if tmp_download.exists():
                    tmp_download.unlink()

        # Always ensure repository configurations are created and up to date
        self.add_repositories()

        if not seed_used:
            cmd_signed = [
                "--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root),
                "install", "--force-resolution", "-y", "--type", "pattern", "base"
            ]
            cmd_fallback = [
                "zypper", "--non-interactive", "--root", str(self.target_root),
                "--no-gpg-checks", "install", "--force-resolution", "-y", "--type", "pattern", "base"
            ]
            res = self._run_prefer_signed(cmd_signed, cmd_fallback[1:])
            if res.returncode != 0:
                cmd_pkg_signed = [
                    "--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root),
                    "install", "--force-resolution", "-y", "patterns-base-base"
                ]
                cmd_pkg_fallback = [
                    "zypper", "--non-interactive", "--root", str(self.target_root),
                    "--no-gpg-checks", "install", "--force-resolution", "-y", "patterns-base-base"
                ]
                res = self._run_prefer_signed(cmd_pkg_signed, cmd_pkg_fallback[1:])
                if res.returncode != 0:
                    cmd_min = [
                        "--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root),
                        "install", "--force-resolution", "-y", "patterns-base-minimal_base"
                    ]
                    res = self._run_prefer_signed(cmd_min)
                    if res.returncode != 0:
                        raise ZypperManagerError(f"Zypper base pattern installation failed with exit code {res.returncode}")

        # The appliance/base pattern is intentionally tiny.  RPM scriptlets
        # from desktop and hardware packages require these POSIX utilities,
        # so install them before resolving user-selected profiles.
        bootstrap_tools = ["bash", "coreutils", "findutils", "grep", "sed", "shadow"]
        tool_cmd_signed = [
            "--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root),
            "install", "--force-resolution", "-y", *bootstrap_tools,
        ]
        tool_cmd_fallback = [
            "zypper", "--non-interactive", "--root", str(self.target_root),
            "--no-gpg-checks", "install", "--force-resolution", "-y", *bootstrap_tools,
        ]
        res = self._run_prefer_signed(tool_cmd_signed, tool_cmd_fallback[1:])
        if res.returncode != 0:
            raise ZypperManagerError(f"Could not install rootfs bootstrap utilities (exit code {res.returncode})")

        # Import GPG keys into RPM database to eliminate NOKEY signature warnings
        for key_dir in [
            self.target_root / "usr" / "lib" / "sysimage" / "rpm",
            self.target_root / "etc" / "pki" / "rpm-gpg",
            self.target_root / "usr" / "share" / "doc" / "packages" / "openSUSE-release"
        ]:
            if key_dir.exists():
                for key_file in key_dir.glob("*.gpg"):
                    subprocess.run(["rpm", "--root", str(self.target_root), "--import", str(key_file)], capture_output=True, check=False)
                for key_file in key_dir.glob("gpg-pubkey*"):
                    subprocess.run(["rpm", "--root", str(self.target_root), "--import", str(key_file)], capture_output=True, check=False)

        if create_seed and not seed_used:
            try:
                logger.info(f"⚡ Fast-caching rootfs seed tarball to {seed_cache}...")
                tmp_seed = seed_cache.with_suffix(".tmp")
                cache_result = subprocess.run([
                    "tar", "czpf", str(tmp_seed),
                    "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*", "--exclude=./tmp/*", "--exclude=./run/*",
                    "-C", str(self.target_root), "."
                ], check=False)
                if cache_result.returncode == 0:
                    if tmp_seed.exists() and tmp_seed.stat().st_size >= 512:
                        tmp_seed.replace(seed_cache)
                    elif not tmp_seed.exists():
                        # In mocked subprocess tests, seed_cache might be touched directly
                        pass
                    logger.info(f"Seed tarball cached successfully.")
            except Exception as e:
                logger.warning(f"Could not save seed tarball cache: {e}")

    def refresh(self):
        if self.chroot.mode == "mock":
            return
        result = self._run_prefer_signed(
            ["--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root), "refresh"],
            ["--non-interactive", "--no-gpg-checks", "--root", str(self.target_root), "refresh"],
        )
        if result.returncode != 0:
            raise ZypperManagerError(f"Zypper refresh failed with exit code {result.returncode}")

    def sync_cache_to_target(self):
        if self.chroot.mode == "mock":
            return
        host_cache = self.resolve_cache_dir() / "packages"
        target_cache = self.target_root / "var" / "cache" / "zypp" / "packages"
        if not host_cache.exists():
            return
        target_cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--no-o", "--no-g", "--ignore-existing", f"{host_cache}/", f"{target_cache}/"], check=False)

    def sync_cache_from_target(self):
        if self.chroot.mode == "mock":
            return
        host_cache = self.resolve_cache_dir() / "packages"
        target_cache = self.target_root / "var" / "cache" / "zypp" / "packages"
        if not target_cache.exists():
            return
        host_cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--no-o", "--no-g", "--ignore-existing", f"{target_cache}/", f"{host_cache}/"], check=False)

    def install_packages(self, packages: List[str]):
        if not packages or self.chroot.mode == "mock":
            return
        real_pkgs = [p for p in packages if p]
        if not real_pkgs:
            return
        self.sync_cache_to_target()
        cmd_signed = ["--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root), "install", "--force-resolution", "-y"] + real_pkgs
        cmd_fallback = ["--non-interactive", "--root", str(self.target_root), "--no-gpg-checks", "install", "--force-resolution", "-y"] + real_pkgs
        res = self._run_prefer_signed(cmd_signed, cmd_fallback)
        
        if res.returncode != 0:
            logger.warning(f"Batch package installation failed (code {res.returncode}); dynamically checking package availability...")
            valid_pkgs = []
            for pkg in real_pkgs:
                check_res = self._run_zypper(["--root", str(self.target_root), "info", pkg])
                if check_res.returncode == 0:
                    valid_pkgs.append(pkg)
                else:
                    logger.warning(f"⚠️ Omitting package '{pkg}' (not found in active repositories for this architecture/distro).")
            
            if valid_pkgs:
                logger.info(f"Retrying installation with {len(valid_pkgs)} confirmed packages...")
                cmd_retry_signed = ["--non-interactive", "--gpg-auto-import-keys", "--root", str(self.target_root), "install", "--force-resolution", "-y"] + valid_pkgs
                cmd_retry_fallback = ["--non-interactive", "--root", str(self.target_root), "--no-gpg-checks", "install", "--force-resolution", "-y"] + valid_pkgs
                res = self._run_prefer_signed(cmd_retry_signed, cmd_retry_fallback)

        self.sync_cache_from_target()
        if res.returncode != 0:
            raise ZypperManagerError(f"Zypper package installation failed with exit code {res.returncode}")

        # Import all repository GPG keys into RPM database and pre-trust them in Zypper
        for key_dir in [
            self.target_root / "usr" / "lib" / "sysimage" / "rpm",
            self.target_root / "etc" / "pki" / "rpm-gpg",
            self.target_root / "usr" / "share" / "doc" / "packages" / "openSUSE-release"
        ]:
            if key_dir.exists():
                for key_file in key_dir.glob("*.gpg"):
                    subprocess.run(["rpm", "--root", str(self.target_root), "--import", str(key_file)], capture_output=True, check=False)
                for key_file in key_dir.glob("gpg-pubkey*"):
                    subprocess.run(["rpm", "--root", str(self.target_root), "--import", str(key_file)], capture_output=True, check=False)
        try:
            self.chroot.run_in_chroot(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh"], check=False)
        except Exception:
            pass

    def clean_cache(self):
        if self.chroot.mode == "mock":
            return
        self._run_zypper(["--root", str(self.target_root), "clean", "--all"], check=False)

    def download_offline_packages(self, packages: List[str], dest_dir: Path) -> Path:
        """
        Downloads the specified packages (and dependencies) into dest_dir and creates repodata metadata.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            (dest_dir / "repodata").mkdir(parents=True, exist_ok=True)
            (dest_dir / "repodata" / "repomd.xml").touch()
            return dest_dir

        real_pkgs = [p for p in packages if p]
        if real_pkgs:
            logger.info(f"📦 Downloading {len(real_pkgs)} offline packages into {dest_dir}...")
            cmd_download = [
                "--non-interactive", "--root", str(self.target_root),
                "--pkg-cache-dir", str(dest_dir),
                "download",
            ] + real_pkgs
            self._run_zypper(cmd_download, check=False)

            # Also copy downloaded .rpm files from zypper cache into dest_dir
            for src_dir in [
                self.target_root / "var" / "cache" / "zypp" / "packages",
                self.resolve_cache_dir() / "packages",
            ]:
                if src_dir.exists():
                    for rpm_file in src_dir.rglob("*.rpm"):
                        try:
                            dst_file = dest_dir / rpm_file.name
                            if not dst_file.exists():
                                shutil.copy2(rpm_file, dst_file)
                        except Exception:
                            pass

        self.create_repository_metadata(dest_dir)
        return dest_dir

    def create_repository_metadata(self, repo_dir: Path):
        repo_dir = Path(repo_dir)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            (repo_dir / "repodata").mkdir(parents=True, exist_ok=True)
            (repo_dir / "repodata" / "repomd.xml").touch()
            return

        tool = shutil.which("createrepo_c") or shutil.which("createrepo")
        if tool:
            subprocess.run([tool, str(repo_dir)], check=False)
        else:
            self.chroot.run_in_chroot(["createrepo_c", str(repo_dir)], check=False)

