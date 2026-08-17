import os
import stat
import sys
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any

from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("hook_manager")


class HookManager:
    """
    Manages and executes user-defined hook scripts at various stages of the build process.
    Supported stages:
      - pre-chroot: Runs on the host before entering the chroot.
      - chroot: Runs inside the chroot environment.
      - post-chroot: Runs on the host after the chroot is fully customized but before image creation.
    """

    def __init__(self, chroot_manager, config: Dict[str, Any]):
        self.chroot = chroot_manager
        self.config = config
        self.hooks_base = resolve_from_project("hooks")
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensures the standard hook directories exist."""
        for stage in ["pre-chroot", "chroot", "post-chroot"]:
            (self.hooks_base / stage).mkdir(parents=True, exist_ok=True)

    def run_stage(self, stage: str):
        """Executes all bash scripts in the specified hook stage directory."""
        stage_dir = self.hooks_base / stage
        if not stage_dir.exists():
            return

        # Find all .sh files, sorted alphabetically for deterministic execution
        scripts = sorted([f for f in stage_dir.glob("*.sh") if f.is_file()])
        if not scripts:
            return

        logger.info(f"⚓ Running hooks for stage: {stage}")
        
        for script in scripts:
            self._ensure_executable(script)
            logger.info(f"  -> Executing hook: {script.name}")
            
            if stage == "chroot":
                self._run_in_chroot(script)
            else:
                self._run_on_host(script)

    def _ensure_executable(self, script: Path):
        """Ensures the script has executable permissions."""
        st = script.stat()
        if not bool(st.st_mode & stat.S_IXUSR):
            logger.info(f"     [Making {script.name} executable]")
            script.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _run_on_host(self, script: Path):
        """Runs the script natively on the host environment."""
        env = os.environ.copy()
        env["TARGET_ROOT"] = str(self.chroot.target_root)
        env["BUILD_ARCH"] = str(self.config.get("arch", ""))
        env["BUILD_DESKTOP"] = str(self.config.get("desktop", ""))
        
        try:
            res = subprocess.run(
                ["/bin/bash", str(script)],
                env=env,
                check=True,
                text=True,
                stdout=sys.stdout if 'sys' in globals() else None,
                stderr=subprocess.STDOUT
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Hook {script.name} failed with exit code {e.returncode}")
            raise RuntimeError(f"Hook {script.name} failed.") from e

    def _run_in_chroot(self, script: Path):
        """Copies the script into the chroot and runs it inside the isolated environment."""
        target_script = self.chroot.target_root / "tmp" / script.name
        
        try:
            # Copy script into the chroot's /tmp directory
            target_script.write_bytes(script.read_bytes())
            target_script.chmod(0o755)
            
            # Execute inside the chroot
            self.chroot.run_in_chroot(
                ["/bin/bash", f"/tmp/{script.name}"], 
                check=True
            )
        except Exception as e:
            logger.error(f"❌ Hook {script.name} failed inside chroot: {e}")
            raise
        finally:
            # Cleanup the injected script
            if target_script.exists():
                target_script.unlink()
