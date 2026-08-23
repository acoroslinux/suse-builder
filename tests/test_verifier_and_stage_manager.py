import pytest
from pathlib import Path
from suse_builder.core.verifier import ImageVerifier, VerificationReport
from suse_builder.core.stage_manager import StageManager


def test_verification_report(tmp_path):
    target = tmp_path / "test.iso"
    target.write_bytes(b"\x00" * 32768 + b"\x01CD001" + b"\x00" * 2048)

    report = ImageVerifier.verify_iso(target)
    assert isinstance(report, VerificationReport)
    assert report.metadata["detected_format"] == "ISO9660 Live CD/DVD Image"
    assert any(c.name == "ISO9660 Signature" and c.passed for c in report.checks)


def test_stage_manager_mock(tmp_path):
    workdir = tmp_path / "workdir"
    target_root = tmp_path / "chroot"
    workdir.mkdir()
    target_root.mkdir()

    sm = StageManager(workdir=workdir, mode="mock", arch="x86_64", distro="leap-15.6")
    tb = sm.resolve_tarball("auto")
    assert isinstance(tb, Path)
    assert tb.exists()

    sm.extract_tarball(tb, target_root)
    assert (target_root / "etc").exists()

    out_tb = tmp_path / "output.tar.zst"
    created = sm.create_stage_tarball(target_root, out_tb)
    assert created.exists()
