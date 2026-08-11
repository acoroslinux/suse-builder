import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("disk_engine")

class DiskEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str):
        self.workdir = Path(workdir)
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode

    def build_disk_image(self) -> Path:
        out_path = resolve_from_project(f"output/{self.output_name}.img")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            out_path.touch()
            return out_path
        if not self.target_root.is_dir():
            raise RuntimeError(f"Root filesystem does not exist: {self.target_root}")
        if shutil.which("mkfs.ext4") is None:
            raise RuntimeError("mkfs.ext4 is required to build a raw ext4 image")
        size = self.config.get("disk_image_size", "4G")
        subprocess.run(["truncate", "-s", str(size), str(out_path)], check=True)
        subprocess.run(["mkfs.ext4", "-F", "-L", self.config.get("system", {}).get("hostname", "suse-rootfs"), "-d", str(self.target_root), str(out_path)], check=True)
        return out_path
