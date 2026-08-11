import tarfile
from pathlib import Path

from suse_builder.core.toolchain_manager import ToolchainManager


def _write_minimal_bootstrap_archive(archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    payload_root = archive_path.parent / "payload"
    (payload_root / "etc").mkdir(parents=True, exist_ok=True)
    (payload_root / "etc" / "os-release").write_text("NAME=openSUSE\n")

    with tarfile.open(archive_path, "w:xz") as archive:
        archive.add(payload_root / "etc", arcname="etc")


def test_bootstrap_build_host_uses_cached_archive(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir" / "x86_64"
    manager = ToolchainManager(workdir_base=workdir, mode="real", force_isolated=True)

    archive_path = manager.cache_dir / "tumbleweed-build-host-x86_64.tar.xz"
    _write_minimal_bootstrap_archive(archive_path)

    download_calls = []

    def fake_urlretrieve(url, destination):
        download_calls.append((url, destination))

    monkeypatch.setattr("suse_builder.core.toolchain_manager.urllib.request.urlretrieve", fake_urlretrieve)

    manager.bootstrap_build_host()

    assert (manager.build_host_dir / ".suse-builder-bootstrap").exists()
    assert not download_calls


def test_bootstrap_build_host_downloads_archive_when_missing(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir" / "x86_64"
    manager = ToolchainManager(workdir_base=workdir, mode="real", force_isolated=True)

    archive_path = manager.cache_dir / "tumbleweed-build-host-x86_64.tar.xz"
    assert not archive_path.exists()

    download_calls = []

    def fake_urlretrieve(url, destination):
        download_calls.append((url, destination))
        _write_minimal_bootstrap_archive(Path(destination))

    monkeypatch.setattr("suse_builder.core.toolchain_manager.urllib.request.urlretrieve", fake_urlretrieve)

    manager.bootstrap_build_host()

    assert archive_path.exists()
    assert (manager.build_host_dir / ".suse-builder-bootstrap").exists()
    assert len(download_calls) == 1
