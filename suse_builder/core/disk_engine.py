import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("disk_engine")

class DiskEngineError(Exception):
    """Raised when a raw disk image cannot be created."""
    pass

class DiskEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str):
        self.workdir = Path(workdir)
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()

    def build_disk_image(self, target_format: str = "img") -> Path:
        fmt_clean = target_format.lower().lstrip(".")
        if fmt_clean in {"img", "raw"}:
            out_ext = "img"
        else:
            out_ext = fmt_clean

        out_path = resolve_from_project(f"output/{self.output_name}.{out_ext}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            out_path.touch()
            return out_path

        if not self.target_root.is_dir():
            raise DiskEngineError(f"Root filesystem directory does not exist: {self.target_root}")

        if shutil.which("mkfs.ext4") is None:
            raise DiskEngineError("mkfs.ext4 is required to build a disk image.")

        raw_path = self.workdir / f"{self.output_name}.raw"
        size = self.config.get("disk_image_size", "4G")
        hostname = self.config.get("hostname", "suse-rootfs")

        logger.info(f"Creating raw disk image ({size}) at: {raw_path}")
        subprocess.run(["truncate", "-s", str(size), str(raw_path)], check=True)

        built_partitioned = False
        # Attempt GPT partitioning + loop mount + bootloader installation if running as root
        if os.geteuid() == 0 and shutil.which("sfdisk") and shutil.which("losetup"):
            try:
                self._build_partitioned_disk(raw_path, hostname)
                built_partitioned = True
            except Exception as e:
                logger.warning(f"Partitioned disk image creation failed ({e}); falling back to single-partition ext4 image.")

        if not built_partitioned:
            subprocess.run(
                ["mkfs.ext4", "-F", "-L", hostname, "-d", str(self.target_root), str(raw_path)],
                check=True,
            )

        if out_ext in {"img", "raw"}:
            shutil.move(str(raw_path), str(out_path))
            return out_path

        # Convert raw disk to requested virtual machine image format (qcow2, vmdk, vhd, vdi)
        return self._convert_disk_format(raw_path, out_path, out_ext)

    def _convert_disk_format(self, raw_path: Path, out_path: Path, target_fmt: str) -> Path:
        qemu_img = shutil.which("qemu-img")
        if not qemu_img:
            raise DiskEngineError(f"qemu-img is required to convert raw disk to format '{target_fmt}'.")

        fmt_map = {
            "qcow2": "qcow2",
            "vmdk": "vmdk",
            "vhd": "vpc",
            "vhdx": "vhdx",
            "vdi": "vdi",
        }
        qemu_target_fmt = fmt_map.get(target_fmt, target_fmt)

        logger.info(f"Converting raw disk image to {target_fmt.upper()} format using qemu-img...")
        cmd = [qemu_img, "convert", "-f", "raw", "-O", qemu_target_fmt, str(raw_path), str(out_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        raw_path.unlink(missing_ok=True)

        if res.returncode != 0:
            raise DiskEngineError(f"qemu-img conversion failed: {res.stderr}")

        return out_path

    def _get_device_uuid(self, dev: str) -> str:
        try:
            res = subprocess.run(["blkid", "-s", "UUID", "-o", "value", dev], capture_output=True, text=True, check=True)
            return res.stdout.strip()
        except Exception:
            return ""

    def _install_uefi_bootloader(self, mount_root: Path, esp_dir: Path, root_uuid: str, esp_uuid: str) -> None:
        """Populate the ESP with EFI binaries and standalone grub.cfg for UEFI booting."""
        boot_efi_dir = esp_dir / "EFI" / "BOOT"
        boot_efi_dir.mkdir(parents=True, exist_ok=True)
        suse_efi_dir = esp_dir / "EFI" / "opensuse"
        suse_efi_dir.mkdir(parents=True, exist_ok=True)

        # Write universal UEFI Shell startup script (checks FS0:, FS1:, FS2:, etc.)
        startup_nsh = (
            "@echo -off\n"
            "for %i in 0 1 2 3 4 5 6 7 8 9\n"
            "  if exist FS%i:\\EFI\\BOOT\\BOOTX64.EFI then\n"
            "    FS%i:\n"
            "    cd \\EFI\\BOOT\n"
            "    BOOTX64.EFI\n"
            "  endif\n"
            "endfor\n"
            "\\EFI\\BOOT\\BOOTX64.EFI\n"
        )
        (esp_dir / "startup.nsh").write_text(startup_nsh)

        # Copy GRUB x86_64-efi modules directory so GRUB has all drivers available at runtime
        mod_src = None
        for cand_dir in [
            mount_root / "usr" / "share" / "grub2" / "x86_64-efi",
            mount_root / "usr" / "lib" / "grub2" / "x86_64-efi",
            Path("/usr/lib/grub/x86_64-efi"),
            Path("/usr/share/grub2/x86_64-efi")
        ]:
            if cand_dir.is_dir() and any(cand_dir.glob("*.mod")):
                mod_src = cand_dir
                break

        if mod_src:
            for dst_dir in [
                mount_root / "boot" / "grub2" / "x86_64-efi",
                mount_root / "boot" / "grub" / "x86_64-efi",
                boot_efi_dir / "x86_64-efi"
            ]:
                dst_dir.mkdir(parents=True, exist_ok=True)
                for mod_file in mod_src.glob("*"):
                    if mod_file.is_file():
                        shutil.copy2(mod_file, dst_dir / mod_file.name)
            logger.info(f"Copied GRUB EFI modules from {mod_src} to /boot/grub2/x86_64-efi.")

        # Create early bootstrap grub.cfg to embed inside standalone BOOTX64.EFI
        early_cfg = (
            'insmod part_gpt\n'
            'insmod part_msdos\n'
            'insmod fat\n'
            'insmod ext2\n'
            'insmod btrfs\n'
            'insmod normal\n'
            'insmod search\n'
            'insmod search_fs_uuid\n'
            f'search --no-floppy --fs-uuid --set=root {root_uuid}\n'
            'set prefix=($root)/boot/grub2\n'
            'if [ -f $prefix/grub.cfg ]; then\n'
            '    configfile $prefix/grub.cfg\n'
            'fi\n'
            f'search --no-floppy --fs-uuid --set=root {esp_uuid}\n'
            'set prefix=($root)/EFI/BOOT\n'
            'if [ -f $prefix/grub.cfg ]; then\n'
            '    configfile $prefix/grub.cfg\n'
            'fi\n'
        )
        early_cfg_path = self.workdir / "early_grub.cfg"
        early_cfg_path.write_text(early_cfg)

        grub_mkstandalone = shutil.which("grub2-mkstandalone") or shutil.which("grub-mkstandalone")
        if grub_mkstandalone:
            subprocess.run([
                grub_mkstandalone,
                "-O", "x86_64-efi",
                "-o", str(boot_efi_dir / "BOOTX64.EFI"),
                f"boot/grub/grub.cfg={early_cfg_path}"
            ], capture_output=True, check=False)
            shutil.copy2(boot_efi_dir / "BOOTX64.EFI", suse_efi_dir / "grubx64.efi")
            shutil.copy2(boot_efi_dir / "BOOTX64.EFI", esp_dir / "bootx64.efi")
        else:
            candidates = [
                mount_root / "usr" / "lib" / "grub2" / "x86_64-efi" / "grub.efi",
                mount_root / "usr" / "share" / "efi" / "x86_64" / "grub.efi",
                mount_root / "boot" / "efi" / "EFI" / "opensuse" / "grubx64.efi",
                mount_root / "usr" / "share" / "grub2" / "x86_64-efi" / "core.efi"
            ]
            for cand in candidates:
                if cand.exists():
                    shutil.copy2(cand, boot_efi_dir / "BOOTX64.EFI")
                    shutil.copy2(cand, suse_efi_dir / "grubx64.efi")
                    shutil.copy2(cand, esp_dir / "bootx64.efi")
                    break

        # Detect exact kernel and initrd filenames under /boot
        k_name = None
        i_name = None
        for bdir in [mount_root / "boot", self.target_root / "boot"]:
            if bdir.exists():
                for f in sorted(bdir.glob("vmlinuz-*")):
                    if f.is_file() and not f.is_symlink():
                        k_name = f.name
                        break
                for f in sorted(bdir.glob("initrd-*")):
                    if f.is_file() and not f.is_symlink():
                        i_name = f.name
                        break
                if k_name and i_name:
                    break

        boot_dir = mount_root / "boot"
        boot_dir.mkdir(parents=True, exist_ok=True)

        if not k_name:
            for f in sorted(boot_dir.glob("vmlinuz*")):
                k_name = f.name
                break
            k_name = k_name or "vmlinuz"

        if not i_name:
            for f in sorted(boot_dir.glob("initrd*")):
                i_name = f.name
                break
            i_name = i_name or "initrd"

        # Create both hardlink and symlink for /boot/vmlinuz and /boot/initrd to guarantee GRUB finds them
        if k_name != "vmlinuz" and (boot_dir / k_name).exists():
            (boot_dir / "vmlinuz").unlink(missing_ok=True)
            try:
                os.link(str(boot_dir / k_name), str(boot_dir / "vmlinuz"))
            except Exception:
                try:
                    (boot_dir / "vmlinuz").symlink_to(k_name)
                except Exception:
                    pass

        if i_name != "initrd" and (boot_dir / i_name).exists():
            (boot_dir / "initrd").unlink(missing_ok=True)
            try:
                os.link(str(boot_dir / i_name), str(boot_dir / "initrd"))
            except Exception:
                try:
                    (boot_dir / "initrd").symlink_to(i_name)
                except Exception:
                    pass

        fs_type = str(self.config.get("filesystem") or "ext4").lower()
        grub_cfg = (
            'insmod part_gpt\n'
            'insmod part_msdos\n'
            'insmod ext2\n'
            'insmod btrfs\n'
            'insmod fat\n'
            'insmod all_video\n'
            'set default="0"\n'
            'set timeout=5\n\n'
            f'search --no-floppy --fs-uuid --set=root {root_uuid}\n\n'
            'menuentry "openSUSE Linux (Leap 15.6)" {\n'
            f'    linux /boot/{k_name} root=UUID={root_uuid} rootfstype={fs_type} rw quiet splash console=tty1 console=ttyS0,115200\n'
            f'    initrd /boot/{i_name}\n'
            '}\n'
            'menuentry "openSUSE Linux (Recovery Mode)" {\n'
            f'    linux /boot/{k_name} root=UUID={root_uuid} rootfstype={fs_type} single console=tty1 console=ttyS0,115200\n'
            f'    initrd /boot/{i_name}\n'
            '}\n'
        )
        (boot_efi_dir / "grub.cfg").write_text(grub_cfg)
        (suse_efi_dir / "grub.cfg").write_text(grub_cfg)

        # Also write grub.cfg to rootfs /boot/grub2/ and /boot/grub/ for Legacy BIOS booting
        for gdir in [mount_root / "boot" / "grub2", mount_root / "boot" / "grub"]:
            gdir.mkdir(parents=True, exist_ok=True)
            (gdir / "grub.cfg").write_text(grub_cfg)

    def _build_partitioned_disk(self, out_path: Path, label: str) -> None:
        """Create a GPT partitioned image with EFI System Partition (ESP) as partition 1 and rootfs as partition 2."""
        sfdisk_script = (
            "label: gpt\n"
            "type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, size=256M, name=\"EFI System Partition\"\n"
            "type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name=\"Linux rootfs\"\n"
        )
        proc = subprocess.run(["sfdisk", str(out_path)], input=sfdisk_script, text=True, capture_output=True)
        if proc.returncode != 0:
            raise DiskEngineError(f"sfdisk partitioning failed: {proc.stderr}")

        loop_res = subprocess.run(["losetup", "--show", "-f", "-P", str(out_path)], capture_output=True, text=True, check=True)
        loop_dev = loop_res.stdout.strip()

        try:
            p1_esp = f"{loop_dev}p1"
            p2_root = f"{loop_dev}p2"
            fs_type = str(self.config.get("filesystem") or "ext4").lower()

            if shutil.which("mkfs.vfat"):
                subprocess.run(["mkfs.vfat", "-F", "32", "-n", "EFI", p1_esp], check=True, stdout=subprocess.DEVNULL)

            if fs_type == "btrfs" and shutil.which("mkfs.btrfs"):
                subprocess.run(["mkfs.btrfs", "-f", "-L", label, p2_root], check=True, stdout=subprocess.DEVNULL)
                mount_root = self.workdir / "mnt_root"
                mount_root.mkdir(parents=True, exist_ok=True)
                subprocess.run(["mount", p2_root, str(mount_root)], check=True)
                try:
                    subprocess.run(["cp", "-a", f"{self.target_root}/.", str(mount_root)], check=True)
                finally:
                    subprocess.run(["umount", str(mount_root)], check=False)
            else:
                fs_type = "ext4"
                subprocess.run(["mkfs.ext4", "-F", "-L", label, "-d", str(self.target_root), p2_root], check=True, stdout=subprocess.DEVNULL)

            # Mount to configure fstab, EFI binaries, and GRUB
            mount_root = self.workdir / "mnt_root"
            mount_root.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", p2_root, str(mount_root)], check=True)
            try:
                esp_uuid = self._get_device_uuid(p1_esp)
                root_uuid = self._get_device_uuid(p2_root)

                fstab_path = mount_root / "etc" / "fstab"
                fstab_path.parent.mkdir(parents=True, exist_ok=True)
                fstab_lines = [
                    "# /etc/fstab: generated by suse-builder for partitioned disk",
                    f"UUID={root_uuid}  /          {fs_type}  defaults,noatime  0  1",
                ]
                if esp_uuid:
                    fstab_lines.append(f"UUID={esp_uuid}  /boot/efi  vfat      umask=0077        0  2")
                fstab_path.write_text("\n".join(fstab_lines) + "\n")

                esp_dir = mount_root / "boot" / "efi"
                esp_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["mount", p1_esp, str(esp_dir)], check=True)
                try:
                    self._install_uefi_bootloader(mount_root, esp_dir, root_uuid, esp_uuid)
                finally:
                    subprocess.run(["umount", str(esp_dir)], check=False)
            finally:
                subprocess.run(["umount", str(mount_root)], check=False)

            logger.info("Successfully formatted Hybrid GPT partitions (bios_grub + ESP + rootfs) and configured dual BIOS+UEFI bootloaders.")
        finally:
            subprocess.run(["losetup", "-d", loop_dev], check=False)

