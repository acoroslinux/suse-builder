import pytest
from pathlib import Path
import tarfile
import pytest
from suse_builder.core.orchestrator import BuildOrchestrator
from suse_builder.core.config_loader import ConfigLoaderError

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

    def test_mock_build_container_is_oci_layout(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, output_format="container")
        result = orch.build(output_name="test-container")
        assert result.name.endswith(".oci.tar")
        with tarfile.open(result) as archive:
            assert {"oci-layout", "index.json"}.issubset(archive.getnames())

    def test_unknown_profile_is_rejected(self):
        with pytest.raises(ConfigLoaderError):
            make_orchestrator(distro="does-not-exist")

    def test_multimedia_codecs_enable_packman(self):
        orch = make_orchestrator(multimedia_codecs=True)
        assert any(repo.get("name") == "packman" for repo in orch.config["repos"])

    def test_fingerprint_changes_with_user_choices(self):
        orch_a = make_orchestrator(multimedia_codecs=False, with_flathub=False)
        orch_b = make_orchestrator(multimedia_codecs=True, with_flathub=True)

        assert orch_a._effective_build_fingerprint() != orch_b._effective_build_fingerprint()

    def test_fingerprint_state_roundtrip(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path)
        fingerprint = orch._effective_build_fingerprint()

        assert orch._load_previous_build_fingerprint() is None
        orch._save_build_fingerprint(fingerprint)

        assert orch._load_previous_build_fingerprint() == fingerprint


def test_orchestrator_tmpfs_fast_benchmark(tmp_path):
    orch = make_orchestrator(
        tmp_path=tmp_path,
        output_format="iso",
        use_tmpfs=True,
        fast=True,
        benchmark=True
    )
    assert orch.use_tmpfs is True
    assert orch.fast is True
    assert orch.benchmark is True
    result = orch.build(output_name="test-tmpfs")
    assert result.exists()
    assert "total" in orch.timings


def test_post_build_cleanup_with_clean_true(tmp_path):
    orch = make_orchestrator(tmp_path=tmp_path, clean=True)
    orch.workdir.mkdir(parents=True, exist_ok=True)
    dummy_file = orch.workdir / "some_file.txt"
    dummy_file.write_text("temporary build file")

    result = orch.build(output_name="test-clean-true")
    assert result.exists()
    # workdir should be cleaned up at the end of the build
    assert not orch.workdir.exists()


def test_post_build_cleanup_with_clean_false(tmp_path):
    orch = make_orchestrator(tmp_path=tmp_path, clean=False)
    orch.workdir.mkdir(parents=True, exist_ok=True)
    dummy_file = orch.workdir / "some_file.txt"
    dummy_file.write_text("reusable build file")

    result = orch.build(output_name="test-clean-false")
    assert result.exists()
    # workdir should NOT be removed when clean=False
    assert orch.workdir.exists()
    assert dummy_file.exists()


def test_post_build_cleanup_on_build_failure(tmp_path, monkeypatch):
    orch = make_orchestrator(tmp_path=tmp_path, clean=True)
    orch.workdir.mkdir(parents=True, exist_ok=True)
    (orch.workdir / "marker.tmp").write_text("build failed midway")

    def broken_stage_mgr(*args, **kwargs):
        raise RuntimeError("Simulated failure in stage manager")

    monkeypatch.setattr("suse_builder.core.orchestrator.StageManager", broken_stage_mgr)

    with pytest.raises(RuntimeError, match="Simulated failure"):
        orch.build(output_name="test-failure")

    # workdir must still be cleaned up on failure when clean=True
    assert not orch.workdir.exists()

