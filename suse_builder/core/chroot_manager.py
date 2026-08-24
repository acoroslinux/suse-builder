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
        native = {"x86_64": {"x86_64", "amd64"}, "i386": {"i386", "i486", "i586", "i686"}, "aarch64": {"aarch64", "arm64"}}
        if self.arch in native.get(host_arch, {host_arch}):
            return

        arch_key = "aarch64" if self.arch in {"aarch64", "arm64"} else self.arch
        qemu_candidates = [
            f"qemu-{arch_key}-static",
            f"qemu-{arch_key}",
            f"qemu-{arch_key}-binfmt"
        ]
        qemu_path = None
        for cand in qemu_candidates:
            which_p = shutil.which(cand)
            if which_p:
                qemu_path = Path(which_p)
                break
            for fallback_dir in ["/usr/bin", "/usr/local/bin"]:
                cand_path = Path(fallback_dir) / cand
                if cand_path.exists():
                    qemu_path = cand_path
                    break
            if qemu_path:
                break

        if qemu_path and qemu_path.exists():
            for dst_name in [qemu_path.name, f"qemu-{arch_key}-static", f"qemu-{arch_key}"]:
                dst = self.target_root / "usr" / "bin" / dst_name
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(qemu_path, dst)
                except Exception:
                    pass
            logger.info(f"Configured foreign emulation with {qemu_path} for {self.arch}.")
        else:
            logger.warning(f"Could not find qemu-user-static for {self.arch}; foreign chroot scriptlets may fail if binfmt is not host-mounted.")

    def ensure_usrmerge_symlinks(self):
        """Ensure standard UsrMerge symlinks (/bin -> usr/bin, /sbin -> usr/sbin, /lib -> usr/lib, /lib64 -> usr/lib64)."""
        if self.mode == "mock":
            return
        for link_name, target in [("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib"), ("lib64", "usr/lib64")]:
            link_path = self.target_root / link_name
            target_path = self.target_root / target
            if target_path.exists() and not link_path.exists() and not link_path.is_symlink():
                try:
                    link_path.symlink_to(target)
                except Exception:
                    pass

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating mounting virtual filesystems.")
            return

        self.target_root.mkdir(parents=True, exist_ok=True)
        self.ensure_usrmerge_symlinks()
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

        policy_rc_d = self.target_root / "usr" / "sbin" / "policy-rc.d"
        policy_rc_d.parent.mkdir(parents=True, exist_ok=True)
        policy_rc_d.write_text("#!/bin/sh\nexit 101\n")
        policy_rc_d.chmod(0o755)

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating unmounting virtual filesystems.")
            return

        policy_rc_d = self.target_root / "usr" / "sbin" / "policy-rc.d"
        if policy_rc_d.exists():
            policy_rc_d.unlink()

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
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK CHROOT EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        env_prefix = ["/usr/bin/env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8"]
        if isinstance(command, str):
            cmd = ["chroot", str(self.target_root)] + env_prefix + ["/bin/sh", "-c", command]
        else:
            cmd = ["chroot", str(self.target_root)] + env_prefix + list(command)

        full_env = os.environ.copy()
        full_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        full_env["LANG"] = "C.UTF-8"
        full_env["LC_ALL"] = "C.UTF-8"
        if env:
            full_env.update(env)

        return subprocess.run(cmd, check=check, env=full_env, capture_output=capture_output, text=text)
