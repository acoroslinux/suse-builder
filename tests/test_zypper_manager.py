import pytest
from pathlib import Path
from suse_builder.core.chroot_manager import ChrootManager
from suse_builder.core.zypper_manager import ZypperManager

class TestZypperManager:
    def test_mock_bootstrap(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="mock")
        zypper = ZypperManager(chroot, config={"distro": "tumbleweed", "architecture": "x86_64"})
        zypper.bootstrap_rootfs("tumbleweed", "x86_64")
        assert target_root.exists()
