import pytest
from pathlib import Path
from suse_builder.core.config_loader import ConfigLoader
from suse_builder.core.orchestrator import BuildOrchestrator
from suse_builder.core.path_utils import resolve_from_project


@pytest.mark.parametrize("device", [
    "pinephone",
    "pinephone-pro",
    "pinetab2",
    "rpi4",
    "rpi5",
    "rockchip-generic",
    "qualcomm-sdm845",
    "apple-silicon",
    "visionfive2",
    "licheepi4a",
    "qemu-riscv64"
])
def test_device_profile_loading(device):
    loader = ConfigLoader()
    global_cfg = resolve_from_project("configs/global_build.json")
    arch = "riscv64" if device in {"visionfive2", "licheepi4a", "qemu-riscv64"} else "aarch64"
    config = loader.assemble_build_config(
        global_config_path=global_cfg,
        architecture=arch,
        distro="tumbleweed",
        device=device,
        desktop="plasma-mobile"
    )
    assert config["device"] == device
    assert "kernel-default" in config["software"]
    assert config["services"]["enable"]


@pytest.mark.parametrize("mobile_desktop", [
    "plasma-mobile",
    "phosh",
    "sxmo"
])
def test_mobile_desktop_profiles(mobile_desktop):
    loader = ConfigLoader()
    global_cfg = resolve_from_project("configs/global_build.json")
    config = loader.assemble_build_config(
        global_config_path=global_cfg,
        architecture="aarch64",
        distro="tumbleweed",
        desktop=mobile_desktop
    )
    assert config["desktop"] == mobile_desktop
    assert "NetworkManager" in config["software"]


def test_orchestrator_mock_arm_mobile_build(tmp_path):
    orchestrator = BuildOrchestrator(
        arch="aarch64",
        config_path="configs/global_build.json",
        mode="mock",
        distro="tumbleweed",
        desktop="plasma-mobile",
        device="pinephone",
        output_format="qcow2"
    )
    orchestrator.workdir = tmp_path / "aarch64"
    orchestrator.target_root = orchestrator.workdir / "chroot"
    report = orchestrator.validate()
    assert report["valid"] is True
    artifact = orchestrator.build(output_name="test-arm-pinephone")
    assert artifact.exists()


def test_orchestrator_mock_riscv64_build(tmp_path):
    orchestrator = BuildOrchestrator(
        arch="riscv64",
        config_path="configs/global_build.json",
        mode="mock",
        distro="tumbleweed",
        desktop="xfce",
        device="visionfive2",
        output_format="qcow2"
    )
    orchestrator.workdir = tmp_path / "riscv64"
    orchestrator.target_root = orchestrator.workdir / "chroot"
    report = orchestrator.validate()
    assert report["valid"] is True
    artifact = orchestrator.build(output_name="test-riscv64-visionfive2")
    assert artifact.exists()
