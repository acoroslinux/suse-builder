import pytest
from pathlib import Path
import subprocess
import tempfile
from suse_builder.core.chroot_manager import ChrootManager
from suse_builder.core.zypper_manager import ZypperManager

class TestZypperManager:
    def test_mock_bootstrap(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="mock")
        zypper = ZypperManager(chroot, config={"distro": "tumbleweed", "architecture": "x86_64"})
        zypper.bootstrap_rootfs("tumbleweed", "x86_64")
        assert target_root.exists()

    def test_resolve_cache_dir_uses_configured_path(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="real", arch="x86_64")
        configured = tmp_path / "custom-cache" / "zypper"
        zypper = ZypperManager(chroot, config={"system": {"zypper_cache": str(configured)}})

        resolved = zypper.resolve_cache_dir()

        assert resolved == configured
        assert resolved.exists()

    def test_resolve_cache_dir_falls_back_to_tmp_on_mkdir_failure(self, tmp_path, monkeypatch):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="real", arch="x86_64")
        zypper = ZypperManager(chroot, config={"system": {"zypper_cache": "broken-cache"}})

        from suse_builder.core import zypper_manager as zm

        original_mkdir = Path.mkdir

        def flaky_mkdir(self, *args, **kwargs):
            if "broken-cache" in str(self):
                raise PermissionError("denied")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(zm.Path, "mkdir", flaky_mkdir)
        resolved = zypper.resolve_cache_dir()

        expected_prefix = Path(tempfile.gettempdir()) / "suse-builder-cache" / "zypper" / "x86_64"
        assert resolved == expected_prefix
        assert resolved.exists()

    def test_bootstrap_uses_seed_cache_when_available(self, tmp_path, monkeypatch):
        target_root = tmp_path / "chroot"
        target_root.mkdir(parents=True)
        chroot = ChrootManager(target_root, mode="real", arch="x86_64")
        cache_dir = tmp_path / "cache" / "x86_64" / "zypper"
        seed_cache = cache_dir.parent / "seed-tumbleweed-x86_64.tar.gz"
        seed_cache.parent.mkdir(parents=True, exist_ok=True)
        seed_cache.write_bytes(b"seed")

        zypper = ZypperManager(chroot, config={"system": {"zypper_cache": str(cache_dir)}})

        zypper_calls = []

        def fake_run_zypper(args, check=False):
            zypper_calls.append(args)
            return subprocess.CompletedProcess(args=args, returncode=0)

        tar_calls = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            tar_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(zypper, "_run_zypper", fake_run_zypper)
        monkeypatch.setattr("suse_builder.core.zypper_manager.subprocess.run", fake_subprocess_run)

        zypper.bootstrap_rootfs("tumbleweed", "x86_64")

        assert any(cmd[:2] == ["tar", "xzpf"] for cmd in tar_calls)
        assert any("sed" in cmd for cmd in zypper_calls)
        assert not any("pattern" in cmd and "base" in cmd for cmd in zypper_calls)

    def test_bootstrap_without_seed_installs_base_and_caches_seed(self, tmp_path, monkeypatch):
        target_root = tmp_path / "chroot"
        target_root.mkdir(parents=True)
        chroot = ChrootManager(target_root, mode="real", arch="x86_64")
        cache_dir = tmp_path / "cache" / "x86_64" / "zypper"
        zypper = ZypperManager(chroot, config={"system": {"zypper_cache": str(cache_dir)}})

        zypper_calls = []

        def fake_run_zypper(args, check=False):
            zypper_calls.append(args)
            return subprocess.CompletedProcess(args=args, returncode=0)

        tar_calls = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            tar_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(zypper, "_run_zypper", fake_run_zypper)
        monkeypatch.setattr("suse_builder.core.zypper_manager.subprocess.run", fake_subprocess_run)

        zypper.bootstrap_rootfs("tumbleweed", "x86_64")

        assert any("pattern" in cmd and "base" in cmd for cmd in zypper_calls)
        assert any("sed" in cmd for cmd in zypper_calls)
        assert any(cmd[:2] == ["tar", "czpf"] for cmd in tar_calls)

    def test_clean_cache_runs_zypper_clean(self, tmp_path, monkeypatch):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="real", arch="x86_64")
        zypper = ZypperManager(chroot, config={})

        calls = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr("suse_builder.core.zypper_manager.subprocess.run", fake_subprocess_run)

        zypper.clean_cache()

        assert calls
        assert calls[0][:4] == ["zypper", "--root", str(target_root), "clean"]
