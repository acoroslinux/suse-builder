import pytest
from pathlib import Path
from suse_builder.core.disk_engine import DiskEngine
from suse_builder.core.container_engine import ContainerEngine


def test_mock_disk_engine(tmp_path):
    workdir = tmp_path / "workdir"
    target_root = tmp_path / "chroot"
    workdir.mkdir()
    target_root.mkdir()

    engine = DiskEngine(
        workdir=workdir,
        target_root=target_root,
        output_name="test-disk",
        config={},
        mode="mock"
    )
    for fmt in ["img", "raw", "qcow2", "vmdk", "vhdx", "vdi"]:
        result = engine.build_disk_image(fmt)
        assert isinstance(result, Path)
        expected_ext = "img" if fmt in {"img", "raw"} else fmt
        assert result.name == f"test-disk.{expected_ext}"


def test_mock_container_engine(tmp_path):
    target_root = tmp_path / "chroot"
    target_root.mkdir()

    engine = ContainerEngine(
        target_root=target_root,
        output_name="test-container",
        config={},
        mode="mock"
    )
    result = engine.build_oci_archive()
    assert isinstance(result, Path)
    assert result.name == "test-container.oci.tar"
    assert result.exists()

