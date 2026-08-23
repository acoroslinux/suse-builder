import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
import logging

from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("stage_manager")


class StageManagerError(Exception):
    """Exception raised for StageManager failures."""
    pass


class StageManager:
    """
    Manages openSUSE base rootfs stage seeds/tarballs for rapid ISO and image creation.
    Allows bootstrapping from pre-built tarballs to skip downloading/installing base packages over network.
    """

    def __init__(self, workdir: Path, mode: str = "mock", arch: str = "x86_64", distro: str = "leap-15.6"):
        self.workdir = Path(workdir).resolve()
        self.mode = mode.lower()
        self.arch = arch
        self.distro = distro
        try:
            self.cache_dir = resolve_from_project("workdir/cache/tarballs")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # Verify writable
            test_file = self.cache_dir / ".write_test"
            test_file.write_text("ok")
            test_file.unlink(missing_ok=True)
        except Exception:
            import tempfile
            self.cache_dir = Path(tempfile.gettempdir()) / "suse-builder-cache" / "tarballs"
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def resolve_tarball(self, tarball_arg: str) -> Path:
        """
        Resolves a user-provided tarball argument (file path, URL, or 'y'/'auto').
        Returns the resolved local Path to the tarball.
        """
        tarball_str = str(tarball_arg).strip()

        # 1. Direct file path or file:// URI
        if tarball_str.startswith("file://"):
            local_path = Path(tarball_str.replace("file://", "")).resolve()
            if not local_path.exists():
                raise StageManagerError(f"Local tarball file not found: {local_path}")
            return local_path

        p = Path(tarball_str)
        if p.exists() and p.is_file():
            return p.resolve()

        # 2. Remote HTTP/HTTPS URL
        if tarball_str.startswith(("http://", "https://")):
            filename = tarball_str.split("/")[-1] or f"suse-base-{self.distro}-{self.arch}.tar.zst"
            target_file = self.cache_dir / filename
            if not target_file.exists():
                logger.info(f"Downloading base tarball from {tarball_str}...")
                if self.mode == "mock":
                    logger.info(f"[MOCK STAGE] Downloading tarball from {tarball_str}")
                    target_file.touch()
                else:
                    try:
                        req = urllib.request.Request(tarball_str, headers={"User-Agent": "SUSE-Builder/1.0"})
                        with urllib.request.urlopen(req) as resp, open(target_file, "wb") as out:
                            shutil.copyfileobj(resp, out)
                    except Exception as e:
                        raise StageManagerError(f"Failed to download tarball from {tarball_str}: {e}")
            return target_file

        # 3. Default/Auto lookup for 'y', 'yes', 'true', 'auto'
        if tarball_str.lower() in ("y", "yes", "true", "auto", "default", "1"):
            candidates = [
                self.cache_dir / f"suse-base-{self.distro}-{self.arch}.tar.zst",
                self.cache_dir / f"suse-base-{self.distro}-{self.arch}.tar.xz",
                self.cache_dir / f"suse-base-{self.distro}-{self.arch}.tar.gz",
                resolve_from_project(f"output/stage_seeds/suse-base-{self.distro}-{self.arch}.tar.zst"),
                resolve_from_project(f"output/suse-base-{self.distro}-{self.arch}.tar.zst"),
                resolve_from_project(f"cache/tarballs/suse-base-{self.distro}-{self.arch}.tar.zst"),
            ]
            for cand in candidates:
                if cand.exists():
                    logger.info(f"Found cached base tarball: {cand}")
                    return cand

            if self.mode == "mock":
                mock_tar = self.cache_dir / f"suse-base-{self.distro}-{self.arch}.tar.zst"
                mock_tar.touch()
                logger.info(f"[MOCK STAGE] Auto-created mock base tarball: {mock_tar}")
                return mock_tar

            raise StageManagerError(
                f"No base tarball found for distro '{self.distro}' and architecture '{self.arch}'. "
                f"Checked: {', '.join(str(c) for c in candidates)}. "
                "Use --create-tarball first to generate one."
            )

        raise StageManagerError(f"Invalid tarball specification: '{tarball_str}'")

    def extract_tarball(self, tarball_path: Path, target_root: Path) -> None:
        """
        Extracts a base tarball into target_root (chroot).
        """
        tarball_path = Path(tarball_path).resolve()
        target_root = Path(target_root).resolve()

        if not tarball_path.exists() and self.mode != "mock":
            raise StageManagerError(f"Tarball file does not exist: {tarball_path}")

        logger.info(f"Extracting base tarball '{tarball_path.name}' into {target_root}...")
        target_root.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            logger.info(f"[MOCK STAGE MANAGER] Extracting tarball {tarball_path} -> {target_root}")
            (target_root / "etc").mkdir(parents=True, exist_ok=True)
            (target_root / "bin").mkdir(parents=True, exist_ok=True)
            (target_root / "var" / "lib" / "rpm").mkdir(parents=True, exist_ok=True)
            return

        if os.geteuid() != 0:
            raise StageManagerError(
                "Extracting base tarballs in real mode requires root privileges (sudo).\n"
                "Please run with sudo: sudo python3 cli.py ..."
            )

        # Detect fastest available parallel decompressor
        decompressor_opt = []
        tb_name = tarball_path.name.lower()
        if tb_name.endswith((".tar.zst", ".tzst")):
            if shutil.which("zstd"):
                decompressor_opt = ["-I", "zstd -T0 -d"]
        elif tb_name.endswith((".tar.xz", ".txz")):
            if shutil.which("pixz"):
                decompressor_opt = ["-I", "pixz"]
        elif tb_name.endswith((".tar.gz", ".tgz")):
            if shutil.which("pigz"):
                decompressor_opt = ["-I", "pigz -d"]

        cmd = ["tar"] + decompressor_opt + ["-xpf", str(tarball_path), "-C", str(target_root), "--numeric-owner", "--xattrs-include=*.*"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Tarball extraction failed: {res.stderr}")
            raise StageManagerError(f"Failed to extract tarball: {res.stderr}")

        logger.info(f"Base tarball successfully extracted to {target_root}.")

    def create_stage_tarball(
        self, source_root: Path, output_tarball: Path, compression: str = "zstd"
    ) -> Path:
        """
        Packages a clean rootfs directory into a stage tarball (.tar.zst, .tar.xz, .tar.gz).
        Automatically leverages multi-core parallel compressors (zstd, pixz, pigz) when present.
        """
        source_root = Path(source_root).resolve()
        output_tarball = Path(output_tarball).resolve()
        output_tarball.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Creating stage tarball '{output_tarball.name}' from {source_root}...")

        if self.mode == "mock":
            logger.info(f"[MOCK STAGE MANAGER] Creating stage tarball {output_tarball}")
            output_tarball.touch()
            return output_tarball

        # Select fastest compressor
        compressor_opt = []
        if compression == "zstd" or output_tarball.name.endswith((".tar.zst", ".zst")):
            if shutil.which("zstd"):
                compressor_opt = ["-I", "zstd -T0 -3"]
            else:
                compressor_opt = ["--zstd"]
        elif compression == "gzip" or output_tarball.name.endswith((".tar.gz", ".gz")):
            if shutil.which("pigz"):
                compressor_opt = ["-I", "pigz"]
            else:
                compressor_opt = ["-z"]
        else:
            if shutil.which("pixz"):
                compressor_opt = ["-I", "pixz"]
            else:
                compressor_opt = ["-J"]

        cmd = [
            "tar", *compressor_opt, "-cpf", str(output_tarball),
            "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
            "--exclude=./run/*", "--exclude=./tmp/*",
            "--exclude=./var/cache/zypp/*",
            "-C", str(source_root), "."
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Stage tarball creation failed: {res.stderr}")
            raise StageManagerError(f"Failed to create stage tarball: {res.stderr}")

        logger.info(f"Stage tarball created successfully: {output_tarball}")
        return output_tarball
