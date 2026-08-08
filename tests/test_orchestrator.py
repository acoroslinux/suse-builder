import pytest
from pathlib import Path
from suse_builder.core.orchestrator import BuildOrchestrator

def make_orchestrator(tmp_path=None, **kwargs) -> BuildOrchestrator:
    defaults = dict(
        arch="x86_64",
        mode="mock",
        distro="tumbleweed",
        desktop=None,
        output_format="iso",
    )
    defaults.update(kwargs)
    orch = BuildOrchestrator(**defaults)
    if tmp_path:
        orch.workdir = tmp_path / orch.arch
        orch.target_root = orch.workdir / "chroot"
    return orch

class TestOrchestrator:
    def test_construction(self):
        orch = make_orchestrator()
        assert orch.arch == "x86_64"
        assert orch.distro == "tumbleweed"

    def test_validate(self):
        orch = make_orchestrator()
        report = orch.validate()
        assert report.get("valid") is True

    def test_mock_build_tumbleweed(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="tumbleweed", desktop="kde")
        result = orch.build()
        assert isinstance(result, Path)

    def test_mock_build_leap(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="leap-15.6", desktop="xfce")
        result = orch.build()
        assert isinstance(result, Path)

    def test_mock_build_tarball(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="tumbleweed", output_format="tarball")
        result = orch.build()
        assert isinstance(result, Path)
        assert result.name.endswith(".tar.xz")
