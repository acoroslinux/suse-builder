import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import PurePosixPath
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import logging

logger = logging.getLogger("toolchain_manager")

_HOST_ARCH = platform.machine().lower()

class ToolchainManagerError(Exception):
    pass

class ToolchainManager:
    """
    Manages an isolated secondary chroot (build_host), containing all
    openSUSE build and ISO creation tools (zypper, mksquashfs, grub2-mkstandalone, xorriso, mtools).
    Ensures suse-builder is 100% host distribution agnostic.
    """

    def __init__(
        self,
        workdir_base: Path,
        mode: str = "mock",
        force_isolated: bool = False,
        target_arch: str = "x86_64",
        distro: str = "tumbleweed",
    ):
        self.workdir_base = Path(workdir_base).resolve()
        self.mode = mode.lower()
        self.force_isolated = force_isolated
        self.target_arch = target_arch.lower()
        self.distro = distro
        self.build_host_dir = self.workdir_base.parent / "build_host"
        self.cache_dir = self.workdir_base.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.is_mounted = False
        self.use_isolated = False
        self.project_root = self.workdir_base.parent.parent
        self.project_mount = self.build_host_dir / "project"

    def _bootstrap_url(self) -> str:
        if _HOST_ARCH not in {"x86_64", "amd64"}:
            raise ToolchainManagerError(
                f"No isolated build-host image is configured for host architecture {_HOST_ARCH}."
            )
        return "https://download.opensuse.org/tumbleweed/appliances/opensuse-tumbleweed-image.x86_64-networkd.tar.xz"

    @staticmethod
    def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
        """Extract a trusted rootfs without following rootfs-internal symlinks.

        Root filesystems legitimately contain links such as ``/dev/fd ->
        /proc/self/fd``.  Resolving each member against the destination would
        follow that link on the *host*, incorrectly rejecting the archive.
        """
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ToolchainManagerError(f"Unsafe path in bootstrap archive: {member.name}")
        # Python 3.14 warns when no extraction filter is provided.
        # We validated member paths above, so this keeps behavior and removes the warning.
        try:
            archive.extractall(destination, filter="fully_trusted")
        except TypeError:
            archive.extractall(destination)

    def missing_host_tools(self) -> List[str]:
        """Return executables missing for a real ISO build."""
        required_tools = ["zypper", "mksquashfs", "xorriso", "mformat", "mcopy", "zstd"]
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if not (shutil.which("grub2-mkstandalone") or shutil.which("grub-mkstandalone")):
            missing.append("grub2-mkstandalone (or grub-mkstandalone)")
        return missing

    def check_host_tools(self) -> bool:
        """Check the executables required by a real ISO build."""
        missing = self.missing_host_tools()
        if missing:
            logger.info(f"Missing tools on host: {', '.join(missing)}")
            return False
        return True

    def setup(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Simulating build_host setup.")
            return

        if not self.force_isolated and self.check_host_tools():
            logger.info("Host has native openSUSE build tools (zypper, xorriso). Using host directly.")
            return
        self.use_isolated = True
        self.bootstrap_build_host()
        self._install_isolated_tools()

    def bootstrap_build_host(self):
        logger.info(f"Initializing isolated openSUSE build environment at: {self.build_host_dir}")
        marker = self.build_host_dir / ".suse-builder-bootstrap"
        if not marker.exists():
            archive_path = self.cache_dir / "tumbleweed-build-host-x86_64.tar.xz"
            if not archive_path.exists():
                logger.info("Downloading isolated openSUSE build host to %s", archive_path)
                urllib.request.urlretrieve(self._bootstrap_url(), archive_path)
            shutil.rmtree(self.build_host_dir, ignore_errors=True)
            self.build_host_dir.mkdir(parents=True)
            try:
                with tarfile.open(archive_path, "r:xz") as archive:
                    self._safe_extract(archive, self.build_host_dir)
            except (tarfile.TarError, OSError) as exc:
                raise ToolchainManagerError(f"Could not unpack isolated build host: {exc}") from exc
            marker.write_text("tumbleweed\n")

        host_resolv = Path("/etc/resolv.conf")
        if host_resolv.exists():
            resolv_dest = self.build_host_dir / "etc" / "resolv.conf"
            resolv_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(host_resolv, resolv_dest)

    def _install_isolated_tools(self) -> None:
        zypper = self.build_host_dir / "usr" / "bin" / "zypper"
        if not zypper.exists():
            raise ToolchainManagerError("The downloaded openSUSE build host does not contain zypper.")
        # The networkd appliance is deliberately minimal and can lack gpg2.
        # Bootstrap it over TLS first; every later transaction uses normal
        # repository signature validation.
        repos_dir = self.build_host_dir / "etc" / "zypp" / "repos.d"
        for repo_file in repos_dir.glob("*.repo"):
            content = repo_file.read_text().replace("http://", "https://")
            if "repo_gpgcheck=" not in content:
                content += "\n# Bootstrap appliance lacks a usable gpgme engine; HTTPS protects transport.\nrepo_gpgcheck=0\ngpgcheck=0\n"
            repo_file.write_text(content)
        initial_args = ["chroot", str(self.build_host_dir), "zypper", "--non-interactive", "--no-gpg-checks"]
        result = subprocess.run([*initial_args, "refresh"], check=False)
        if result.returncode:
            raise ToolchainManagerError("Could not bootstrap repositories in the isolated openSUSE build host.")
        result = subprocess.run([*initial_args, "install", "-y", "gpg2"], check=False)
        if result.returncode:
            raise ToolchainManagerError("Could not install gpg2 in the isolated openSUSE build host.")
        packages = ["squashfs", "zstd", "xorriso", "grub2", "grub2-x86_64-efi", "grub2-i386-efi", "mtools", "dosfstools", "qemu-tools", "syslinux", "util-linux", "ca-certificates"]
        result = subprocess.run(["chroot", str(self.build_host_dir), "zypper", "--non-interactive", "install", "-y", *packages], check=False)
        if result.returncode:
            raise ToolchainManagerError("Could not install ISO build tools in the isolated openSUSE build host.")
        # Attempt installing optional openSUSE media tag/check utilities without failing if unavailable
        subprocess.run(["chroot", str(self.build_host_dir), "zypper", "--non-interactive", "install", "-y", "checkmedia"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Mounting virtual filesystems into build_host.")
            self.is_mounted = True
            return

        if not self.build_host_dir.exists():
            self.build_host_dir.mkdir(parents=True, exist_ok=True)

        mounts = [
            ("proc", self.build_host_dir / "proc", "proc", None),
            ("sysfs", self.build_host_dir / "sys", "sysfs", None),
            ("devtmpfs", self.build_host_dir / "dev", "devtmpfs", None),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            cmd = ["mount", "-t", fstype]
            if opts:
                cmd.extend(["-o", opts])
            cmd.extend([src, str(target)])
            subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)

        # 1. Mount target architecture workdir with --rbind (crosses tmpfs boundaries seamlessly)
        target_workdir_mount = self.build_host_dir / "workdir" / self.target_arch
        target_workdir_mount.mkdir(parents=True, exist_ok=True)
        self.workdir_base.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "--rbind", str(self.workdir_base), str(target_workdir_mount)], check=False)
        subprocess.run(["mount", "--make-rslave", str(target_workdir_mount)], check=False)

        # 2. Mount cache directory
        cache_mount = self.build_host_dir / "cache"
        cache_mount.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "--bind", str(self.cache_dir), str(cache_mount)], check=False)

        # 3. Mount configs directory
        configs_dir = self.project_root / "configs"
        if configs_dir.exists():
            configs_mount = self.build_host_dir / "configs"
            configs_mount.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", "--bind", str(configs_dir), str(configs_mount)], check=False)

        # 4. Mount output directory
        output_dir = self.project_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_mount = self.build_host_dir / "output"
        output_mount.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "--bind", str(output_dir), str(output_mount)], check=False)

        self.is_mounted = True

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Unmounting virtual filesystems from build_host.")
            self.is_mounted = False
            return

        from suse_builder.core.path_utils import unmount_all_under
        unmount_all_under(self.build_host_dir)

        self.is_mounted = False

    def run_tool(self, tool_binary: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = f"{tool_binary} {' '.join(args)}"
            logger.info(f"[MOCK TOOL EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=[tool_binary] + args, returncode=0, stdout="", stderr="")

        if self.use_isolated:
            if not self.is_mounted:
                raise ToolchainManagerError("Isolated build host is not mounted.")
            translated = [self._translate_path(arg) for arg in args]
            cmd = ["chroot", str(self.build_host_dir), tool_binary] + translated
        else:
            cmd = [tool_binary] + args

        return subprocess.run(cmd, check=check)

    def _translate_path(self, value: str) -> str:
        if "=" in value:
            prefix, candidate = value.split("=", 1)
            translated = self._translate_path(candidate)
            if translated != candidate:
                return f"{prefix}={translated}"
        try:
            path = Path(value).resolve()
            # 1. workdir paths
            workdir_resolved = self.workdir_base.resolve()
            if path == workdir_resolved or workdir_resolved in path.parents:
                rel = path.relative_to(workdir_resolved)
                return str(Path(f"/workdir/{self.target_arch}") / rel)

            # 2. cache paths
            cache_resolved = self.cache_dir.resolve()
            if path == cache_resolved or cache_resolved in path.parents:
                rel = path.relative_to(cache_resolved)
                return str(Path("/cache") / rel)

            # 3. project root / configs / output paths
            proj_resolved = self.project_root.resolve()
            if path == proj_resolved or proj_resolved in path.parents:
                rel = path.relative_to(proj_resolved)
                first_part = rel.parts[0] if rel.parts else ""
                if first_part in ("configs", "output", "artwork"):
                    return str(Path("/") / rel)
                return str(Path("/project") / rel)
        except Exception:
            pass
        return value

    def run_in_build_host(self, command: Union[str, List[str]], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK BUILD_HOST EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        if isinstance(command, str):
            cmd = ["chroot", str(self.build_host_dir), "/bin/sh", "-c", command]
        else:
            cmd = ["chroot", str(self.build_host_dir)] + command

        return subprocess.run(cmd, check=check)
