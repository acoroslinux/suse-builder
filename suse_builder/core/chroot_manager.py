import os
import shutil
import subprocess
import platform
from pathlib import Path
from typing import Optional, List, Union
import logging

logger = logging.getLogger("chroot_manager")

class ChrootManagerError(Exception):
    """Exception raised for errors in ChrootManager."""
    pass

class ChrootManager:
    def __init__(self, target_root: Path, mode: str = "mock", cache_dir: Optional[Path] = None, arch: str = "x86_64"):
        self.target_root = Path(target_root).resolve()
        self.mode = mode.lower()
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self.arch = arch.lower()
        self.virtual_mounts = ["proc", "sys", "dev", "dev/pts"]

    def prepare_emulation(self) -> None:
        """Make a foreign-architecture rootfs executable through host binfmt/QEMU."""
        if self.mode == "mock":
            return
        host_arch = platform.machine().lower()
        native = {"x86_64": {"x86_64", "amd64"}, "i386": {"i386", "i486", "i586", "i686"}}
        if self.arch in native.get(host_arch, {host_arch}):
            return
        qemu_names = {"aarch64": "qemu-aarch64-static", "riscv64": "qemu-riscv64-static", "i586": "qemu-i386-static", "i686": "qemu-i386-static"}
        qemu_name = qemu_names.get(self.arch)
        qemu_path = shutil.which(qemu_name) if qemu_name else None
        if not qemu_path:
            raise ChrootManagerError(f"Foreign architecture {self.arch} requires {qemu_name}; install or provide qemu-user-static.")
        binfmt_entry = Path("/proc/sys/fs/binfmt_misc") / qemu_name.removesuffix("-static")
        if not binfmt_entry.exists():
            raise ChrootManagerError(f"{qemu_name} is installed but binfmt_misc is not registered. Enable it with: sudo update-binfmts --enable {qemu_name.removesuffix('-static')}")
        destination = self.target_root / "usr" / "bin" / qemu_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(qemu_path, destination)

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating mounting virtual filesystems.")
            return

        self.target_root.mkdir(parents=True, exist_ok=True)
        mounts = [
            ("proc", self.target_root / "proc", "proc", None),
            ("sysfs", self.target_root / "sys", "sysfs", None),
            ("devtmpfs", self.target_root / "dev", "devtmpfs", None),
            ("devpts", self.target_root / "dev" / "pts", "devpts", None),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            cmd = ["mount", "-t", fstype]
            if opts:
                cmd.extend(["-o", opts])
            cmd.extend([src, str(target)])
            subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating unmounting virtual filesystems.")
            return

        for path in [
            self.target_root / "dev" / "pts",
            self.target_root / "dev",
            self.target_root / "sys",
            self.target_root / "proc",
        ]:
            if path.exists():
                subprocess.run(["umount", "-l", str(path)], check=False, stderr=subprocess.DEVNULL)

    def run_in_chroot(
        self,
        command: Union[str, List[str]],
        check: bool = True,
        env: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK CHROOT EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        if isinstance(command, str):
            cmd = ["chroot", str(self.target_root), "/bin/sh", "-c", command]
        else:
            cmd = ["chroot", str(self.target_root)] + command

        full_env = os.environ.copy()
        full_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if env:
            full_env.update(env)

        return subprocess.run(cmd, check=check, env=full_env)
