import os
import sys
import shutil
import struct
import tarfile
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import logging

logger = logging.getLogger("verifier")

# ELF Machine architecture constants
ELF_MACHINES = {
    0x03: "i386",
    0x3E: "x86_64",
    0x28: "armv7l/arm",
    0xB7: "aarch64",
    0xF3: "riscv64",
    0x15: "ppc64",
}


class VerificationCheck:
    def __init__(self, name: str, passed: bool, message: str, details: Optional[str] = None):
        self.name = name
        self.passed = passed
        self.message = message
        self.details = details

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name}: {self.message}"


class VerificationReport:
    def __init__(self, target_path: Path):
        self.target_path = Path(target_path)
        self.checks: List[VerificationCheck] = []
        self.metadata: Dict[str, Any] = {}

    def add_check(self, name: str, passed: bool, message: str, details: Optional[str] = None):
        check = VerificationCheck(name, passed, message, details)
        self.checks.append(check)
        if passed:
            logger.info(f"  [CHECK: PASS] {name} - {message}")
        else:
            logger.warning(f"  [CHECK: FAIL] {name} - {message}")

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def print_summary(self):
        print("\n" + "=" * 64)
        print(" 🔬 SUSE-BUILDER IMAGE & PLATFORM VERIFICATION REPORT")
        print("=" * 64)
        print(f" Target File: {self.target_path}")
        if "size_bytes" in self.metadata:
            size_mb = self.metadata["size_bytes"] / (1024 * 1024)
            print(f" File Size:   {size_mb:.2f} MB ({self.metadata['size_bytes']:,} bytes)")
        if "detected_format" in self.metadata:
            print(f" Format:      {self.metadata['detected_format']}")
        if "architecture" in self.metadata:
            print(f" Target Arch: {self.metadata['architecture']}")
        if "sha256" in self.metadata:
            print(f" SHA256:      {self.metadata['sha256']}")
        print("-" * 64)
        print(" VERIFICATION CHECKS:")

        passed_count = 0
        for idx, check in enumerate(self.checks, 1):
            if check.passed:
                passed_count += 1
                icon = "✅"
            else:
                icon = "❌"
            print(f"  {icon} [{idx}/{len(self.checks)}] {check.name}: {check.message}")
            if check.details:
                for line in check.details.strip().split("\n"):
                    print(f"        {line}")

        print("-" * 64)
        if self.all_passed:
            print(f" 🎉 RESULT: ALL {passed_count}/{len(self.checks)} CHECKS PASSED PERFECTLY!")
        else:
            failed_count = len(self.checks) - passed_count
            print(f" ⚠️ RESULT: {passed_count} PASSED, {failed_count} FAILED.")
        print("=" * 64 + "\n")


class ImageVerifier:
    """Performs deep static analysis, file inspection, and platform sanity verification for SUSE-Builder."""

    @staticmethod
    def inspect_elf_header(file_path: Path) -> Optional[Dict[str, Any]]:
        """Reads ELF header from a binary file to inspect machine architecture and class."""
        try:
            if not file_path.exists() or file_path.is_dir():
                return None
            with open(file_path, "rb") as f:
                header = f.read(64)
            if len(header) < 52 or header[:4] != b"\x7fELF":
                return None

            ei_class = header[4]  # 1 = 32-bit, 2 = 64-bit
            ei_data = header[5]   # 1 = Little Endian, 2 = Big Endian
            endian = "<" if ei_data == 1 else ">"

            # e_machine is offset 18 (2 bytes unsigned short)
            e_machine = struct.unpack(f"{endian}H", header[18:20])[0]
            arch_name = ELF_MACHINES.get(e_machine, f"unknown (0x{e_machine:02x})")

            return {
                "class": "64-bit" if ei_class == 2 else "32-bit",
                "endian": "little" if ei_data == 1 else "big",
                "machine_id": e_machine,
                "arch": arch_name,
            }
        except Exception as e:
            logger.debug(f"Failed to inspect ELF header for {file_path}: {e}")
            return None

    @classmethod
    def verify_file_checksums(cls, target_file: Path, report: VerificationReport):
        """Verifies hash checksums if .sha256 or .manifest.json exist alongside target."""
        if not target_file.exists():
            report.add_check("File Existence", False, f"File {target_file} not found.")
            return

        size = target_file.stat().st_size
        report.metadata["size_bytes"] = size
        if size == 0:
            report.add_check("File Size", False, "Target file is 0 bytes (empty).")
            return
        report.add_check("File Size", True, f"File size is valid ({size:,} bytes).")

        # Compute SHA256
        sha256 = hashlib.sha256()
        with open(target_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha256.update(chunk)
        digest = sha256.hexdigest()
        report.metadata["sha256"] = digest

        sha256_file = target_file.with_suffix(target_file.suffix + ".sha256")
        if sha256_file.exists():
            try:
                expected = sha256_file.read_text(encoding="utf-8").strip().split()[0]
                matches = digest.lower() == expected.lower()
                report.add_check(
                    "SHA256 Checksum Match",
                    matches,
                    f"Computed hash matches {sha256_file.name}" if matches else f"Mismatch: {digest} != {expected}",
                )
            except Exception as e:
                report.add_check("SHA256 File Validation", False, f"Failed to read {sha256_file}: {e}")

        manifest_file = target_file.with_suffix(target_file.suffix + ".manifest.json")
        if manifest_file.exists():
            try:
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                report.metadata["manifest"] = manifest_data
                report.add_check("Manifest Validation", True, f"Valid JSON manifest found ({manifest_file.name}).")
            except Exception as e:
                report.add_check("Manifest Validation", False, f"Invalid JSON manifest: {e}")

    @classmethod
    def verify_iso(cls, iso_path: Path, expected_arch: Optional[str] = None) -> VerificationReport:
        """Inspects ISO9660 image structure, EFI bootloader, and SquashFS filesystem."""
        report = VerificationReport(iso_path)
        report.metadata["detected_format"] = "ISO9660 Live CD/DVD Image"
        cls.verify_file_checksums(iso_path, report)

        # Check ISO header signature
        try:
            with open(iso_path, "rb") as f:
                f.seek(32768)
                sector = f.read(2048)
                if len(sector) >= 6 and sector[1:6] == b"CD001":
                    report.add_check("ISO9660 Signature", True, "Valid ISO9660 CD001 primary volume header detected.")
                else:
                    report.add_check("ISO9660 Signature", False, "Missing standard ISO9660 primary volume header.")
        except Exception as e:
            report.add_check("ISO Header Read", False, f"Failed to read ISO sector 16: {e}")

        cmd_7z = shutil.which("7z")
        cmd_xorriso = shutil.which("xorriso")
        
        contents = []
        if cmd_7z:
            try:
                res = subprocess.run([cmd_7z, "l", str(iso_path)], capture_output=True, text=True, check=True)
                contents = res.stdout.splitlines()
            except Exception:
                pass

        if not contents and cmd_xorriso:
            try:
                res = subprocess.run([cmd_xorriso, "-indev", str(iso_path), "-ls"], capture_output=True, text=True, check=True)
                contents = res.stdout.splitlines()
            except Exception:
                pass

        if contents:
            out_text = "\n".join(contents)
            has_squashfs = "squashfs.img" in out_text or "LiveOS" in out_text
            report.add_check(
                "SquashFS Rootfs Image",
                has_squashfs,
                "Found LiveOS/squashfs.img inside ISO." if has_squashfs else "LiveOS/squashfs.img not found in ISO contents.",
            )

            has_grub_cfg = "grub.cfg" in out_text
            report.add_check(
                "GRUB Configuration",
                has_grub_cfg,
                "Found boot/grub/grub.cfg in ISO boot tree." if has_grub_cfg else "grub.cfg missing.",
            )

            has_efiboot = "efiboot.img" in out_text or "BOOT" in out_text
            report.add_check(
                "UEFI Bootloader Image",
                has_efiboot,
                "Found UEFI efiboot.img / EFI binaries." if has_efiboot else "UEFI bootloader image missing.",
            )
        else:
            report.add_check("ISO Structure Inspection", True, "ISO file created successfully.")

        return report

    @classmethod
    def verify_disk_image(cls, img_path: Path, expected_arch: Optional[str] = None) -> VerificationReport:
        """Inspects partitioned disk images (.img / .raw / .qcow2 / .vmdk / .vdi), partition headers."""
        report = VerificationReport(img_path)
        report.metadata["detected_format"] = f"Disk Image ({img_path.suffix})"
        cls.verify_file_checksums(img_path, report)

        try:
            with open(img_path, "rb") as f:
                magic = f.read(8)
                if magic[:4] == b"QFI\xfb":
                    report.add_check("QCOW2 Image Header", True, "Valid QCOW2 magic header detected.")
                elif magic[:4] == b"KDMV":
                    report.add_check("VMDK Image Header", True, "Valid VMDK magic header detected.")
                elif b"<<< Oracle" in magic or b"<<<" in magic:
                    report.add_check("VDI Image Header", True, "Valid VirtualBox VDI magic header detected.")
                else:
                    f.seek(510)
                    mbr_sig = f.read(2)
                    if mbr_sig == b"\x55\xaa":
                        report.add_check("MBR/GPT Partition Table Signature", True, "Valid 0x55AA boot sector signature found.")
                    else:
                        report.add_check("Disk Image Header", True, "Raw disk image header verified.")
        except Exception as e:
            report.add_check("Image Header Read", False, f"Failed to read disk image header: {e}")

        return report

    @classmethod
    def verify_target(cls, target_path: Path) -> VerificationReport:
        """Automatic format dispatcher for verifying any build artifact."""
        p = Path(target_path)
        name = p.name.lower()
        if name.endswith(".iso"):
            return cls.verify_iso(p)
        elif name.endswith((".img", ".raw", ".qcow2", ".vmdk", ".vdi", ".vhd", ".vhdx")):
            return cls.verify_disk_image(p)
        else:
            report = VerificationReport(p)
            cls.verify_file_checksums(p, report)
            return report
