import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Tuple, Any, Optional
import logging
from suse_builder.core.toolchain_manager import ToolchainManager
from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("iso_engine")

_ARCH_EFI_MAP = {
    "x86_64":  ("x86_64-efi",  "BOOTX64.EFI"),
    "i686":    ("x86_64-efi",  "BOOTX64.EFI"),
    "i586":    ("x86_64-efi",  "BOOTX64.EFI"),
    "aarch64": ("arm64-efi",   "BOOTAA64.EFI"),
    "riscv64": ("riscv64-efi", "BOOTRISCV64.EFI"),
}

class ISOEngineError(Exception):
    pass

class ISOEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str, toolchain: ToolchainManager):
        self.workdir = Path(workdir)
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()
        self.toolchain = toolchain
        self.iso_staging = self.workdir / "iso_root"
        self.arch = config.get("architecture", "x86_64")

    def get_bootloader_type(self) -> str:
        bootloader = self.config.get("bootloader", {})
        if isinstance(bootloader, str):
            raw_type = bootloader
        else:
            raw_type = bootloader.get("type") if isinstance(bootloader, dict) else None
        if not raw_type:
            raw_type = self.config.get("bootloader_type") or self.config.get("boot", {}).get("type") or "grub2-hybrid"

        normalized = str(raw_type).strip().lower().replace("_", "-")
        type_map = {
            "grub2-hybrid": "grub2-hybrid",
            "hybrid": "grub2-hybrid",
            "grub2-uefi": "grub2-uefi",
            "uefi": "grub2-uefi",
            "efi": "grub2-uefi",
            "grub2-bios": "grub2-bios",
            "bios": "grub2-bios",
            "syslinux": "syslinux",
            "isolinux": "syslinux",
        }
        return type_map.get(normalized, "grub2-hybrid")

    def should_use_grub_efi(self) -> bool:
        return self.get_bootloader_type() in {"grub2-hybrid", "grub2-uefi"}

    def should_use_grub_bios(self) -> bool:
        return self.get_bootloader_type() in {"grub2-hybrid", "grub2-bios"}

    def should_use_syslinux(self) -> bool:
        return self.get_bootloader_type() == "syslinux"

    def _get_iso_label(self) -> str:
        raw_label = self.config.get("iso_label", self.config.get("system", {}).get("iso_label", "OPENSUSE_MODERN"))
        sanitized = re.sub(r"[^A-Z0-9_]+", "_", raw_label.upper().strip())
        sanitized = sanitized.strip("_")
        return (sanitized or "OPENSUSE_MODERN")[:32]

    def _get_kernel_params(self) -> str:
        iso_label = self._get_iso_label()
        default_params = f"root=live:CDLABEL={iso_label} rd.live.image rd.live.dir=LiveOS rd.live.squashimg=squashfs.img rd.live.overlay.overlayfs=1 quiet splash"
        return self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", default_params))

    def _find_kernel_and_initramfs(self) -> Tuple[Optional[str], Optional[str]]:
        boot_dir = self.target_root / "boot"
        kernel = None
        initramfs = None

        if boot_dir.exists():
            for f in sorted(boot_dir.iterdir()):
                if f.is_file():
                    if f.name.startswith("vmlinuz"):
                        kernel = f.name
                    elif f.name.startswith("initrd"):
                        initramfs = f.name

        return kernel, initramfs

    def _create_squashfs(self, source_dir: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            output_path.touch()
            return

        if output_path.exists():
            output_path.unlink()

        compression = self.config.get("compression", "zstd")
        num_cpus = os.cpu_count() or 4
        logger.info(f"Creating SquashFS with {compression} compression using {num_cpus} cores...")
        self.toolchain.run_tool(
            "mksquashfs",
            [
                str(source_dir),
                str(output_path),
                "-comp", compression,
                "-b", "1M",
                "-processors", str(num_cpus),
                "-noappend",
                "-e", "proc", "sys", "dev", "tmp", "var/cache/zypp"
            ],
        )

    def generate_grub_efi_image(self):
        efiboot_img = self.iso_staging / "boot" / "grub2" / "efiboot.img"
        efiboot_img.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            efiboot_img.touch()
            return

        formats_to_build = []
        if self.arch in ["i686", "i386", "x86"]:
            formats_to_build = [
                ("i386-efi",   "BOOTIA32.EFI"),
                ("x86_64-efi", "BOOTX64.EFI"),
            ]
        elif self.arch in ["x86_64", "amd64"]:
            formats_to_build = [
                ("x86_64-efi", "BOOTX64.EFI"),
                ("i386-efi",   "BOOTIA32.EFI"),
            ]
        else:
            primary_fmt, primary_file = _ARCH_EFI_MAP.get(self.arch, ("x86_64-efi", "BOOTX64.EFI"))
            formats_to_build = [(primary_fmt, primary_file)]

        efi_tmp = self.workdir / "tmp_efi"
        efi_tmp.mkdir(parents=True, exist_ok=True)

        # Standalone EFI binaries need an embedded early config that can find
        # the ISO and chainload the real menu file from /boot/grub2/grub.cfg.
        embed_cfg = efi_tmp / "embedded-grub.cfg"
        iso_label = self._get_iso_label()
        embed_cfg.write_text(
            "insmod search\n"
            "insmod search_fs_file\n"
            "insmod search_label\n"
            "insmod iso9660\n"
            "insmod configfile\n"
            "if search --no-floppy --set=root --file /boot/mbrid; then\n"
            "    set prefix=($root)/boot/grub2\n"
            "    if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub2/grub.cfg\n"
            "    fi\n"
            "    if [ -f ($root)/boot/grub/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub/grub.cfg\n"
            "    fi\n"
            "fi\n"
            f"if search --no-floppy --set=root --label {iso_label}; then\n"
            "    if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub2/grub.cfg\n"
            "    fi\n"
            "    if [ -f ($root)/boot/grub/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub/grub.cfg\n"
            "    fi\n"
            "fi\n"
            "if search --no-floppy --set=root --file /boot/grub2/grub.cfg; then\n"
            "    configfile ($root)/boot/grub2/grub.cfg\n"
            "fi\n"
            "if search --no-floppy --set=root --file /boot/grub/grub.cfg; then\n"
            "    configfile ($root)/boot/grub/grub.cfg\n"
            "fi\n"
        )

        created_binaries = []
        for fmt, boot_filename in formats_to_build:
            out_binary = efi_tmp / boot_filename
            built = False
            grub_mk = "grub2-mkstandalone" if (self.toolchain.use_isolated or shutil.which("grub2-mkstandalone")) else ("grub-mkstandalone" if shutil.which("grub-mkstandalone") else None)
            if grub_mk:
                res = self.toolchain.run_tool(grub_mk, [
                    f"--format={fmt}",
                    "--fonts=",
                    "--locales=",
                    "--themes=",
                    "--install-modules=iso9660 search search_fs_file search_label configfile normal linux",
                    "--modules=iso9660 search search_fs_file search_label configfile normal linux",
                    "-o", str(out_binary), f"boot/grub/grub.cfg={embed_cfg}"
                ], check=False)
                if res.returncode == 0 and out_binary.exists() and out_binary.stat().st_size > 0:
                    built = True
                elif out_binary.exists():
                    out_binary.unlink(missing_ok=True)

            if built or (out_binary.exists() and out_binary.stat().st_size > 0):
                created_binaries.append((out_binary, boot_filename))

        if created_binaries:
            iso_efi_dir = self.iso_staging / "EFI" / "BOOT"
            iso_efi_dir.mkdir(parents=True, exist_ok=True)
            for binary_path, filename in created_binaries:
                shutil.copy2(binary_path, iso_efi_dir / filename)

            # The efiboot.img must be a valid FAT filesystem. xorriso with
            # -isohybrid-gpt-basdat exposes it as the ESP partition content in
            # the ISO's GPT, so no partition table is needed inside the image.
            total_size_mb = max(32, (sum(p.stat().st_size for p, _ in created_binaries) // (1024 * 1024)) + 16)
            size_kb = total_size_mb * 1024

            if efiboot_img.exists():
                efiboot_img.unlink()

            mkfs_cmd = shutil.which("mkfs.vfat") or shutil.which("mkfs.msdos")
            if mkfs_cmd:
                self.toolchain.run_tool(mkfs_cmd, ["-C", str(efiboot_img), str(size_kb)], check=True)
            else:
                self.toolchain.run_tool("truncate", ["-s", f"{total_size_mb}M", str(efiboot_img)], check=True)
                self.toolchain.run_tool("mformat", ["-i", str(efiboot_img), "-h", "32", "-t", "32", "-n", "64", "-c", "1", "::"], check=True)

            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI"], check=True)
            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI/BOOT"], check=True)
            for binary_path, filename in created_binaries:
                self.toolchain.run_tool("mcopy", ["-i", str(efiboot_img), str(binary_path), f"::/EFI/BOOT/{filename}"], check=True)

        if not efiboot_img.exists() or efiboot_img.stat().st_size == 0:
            raise ISOEngineError("Could not create an EFI boot image; install GRUB and mtools support for the target architecture.")

        shutil.rmtree(efi_tmp, ignore_errors=True)

    def generate_grub_bios_core(self):
        """Build a standalone GRUB core.img for legacy BIOS boot.

        The resulting core.img embeds a config that locates the ISO root and
        chains to /boot/grub2/grub.cfg, and is referenced by the El Torito
        BIOS boot entry in the ISO.
        """
        core_img = self.iso_staging / "boot" / "grub2" / "core.img"
        core_img.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            core_img.touch()
            return

        if core_img.exists() and core_img.stat().st_size > 0:
            return

        grub_mk = "grub2-mkstandalone" if (self.toolchain.use_isolated or shutil.which("grub2-mkstandalone")) else ("grub-mkstandalone" if shutil.which("grub-mkstandalone") else None)
        if not grub_mk:
            logger.warning("grub2-mkstandalone not found; skipping legacy BIOS boot image.")
            return

        iso_label = self._get_iso_label()
        embed_cfg = self.workdir / "tmp_bios_grub.cfg"
        embed_cfg.write_text(
            "insmod search\n"
            "insmod search_fs_file\n"
            "insmod search_label\n"
            "insmod iso9660\n"
            "insmod configfile\n"
            "if search --no-floppy --set=root --file /boot/mbrid; then\n"
            "    set prefix=($root)/boot/grub2\n"
            "    if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub2/grub.cfg\n"
            "    fi\n"
            "fi\n"
            f"if search --no-floppy --set=root --label {iso_label}; then\n"
            "    if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "        configfile ($root)/boot/grub2/grub.cfg\n"
            "    fi\n"
            "fi\n"
        )

        res = self.toolchain.run_tool(
            grub_mk,
            [
                "--format=i386-pc",
                "--fonts=",
                "--locales=",
                "--themes=",
                "--install-modules=iso9660 search search_fs_file search_label configfile normal biosdisk part_msdos linux",
                "--modules=iso9660 search search_fs_file search_label configfile normal biosdisk part_msdos linux",
                "-o", str(core_img),
                f"boot/grub/grub.cfg={embed_cfg}",
            ],
            check=False,
        )
        if res.returncode != 0 or not core_img.exists() or core_img.stat().st_size == 0:
            if core_img.exists():
                core_img.unlink(missing_ok=True)
            logger.warning("Failed to build legacy BIOS core.img; ISO will only boot via UEFI.")
        embed_cfg.unlink(missing_ok=True)

    def build_iso(self) -> Path:
        self.iso_staging.mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "LiveOS").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub2").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub").mkdir(parents=True, exist_ok=True)

        # KIWI uses an early-boot search marker in /boot. Keep a stable marker
        # so standalone GRUB can reliably locate the ISO root.
        (self.iso_staging / "boot" / "mbrid").write_text("suse-builder\n")

        kernel, initramfs = self._find_kernel_and_initramfs()
        if self.mode == "mock":
            kernel = kernel or "vmlinuz"
            initramfs = initramfs or "initrd"
        else:
            if not kernel:
                raise ISOEngineError(
                    f"No kernel found in {self.target_root / 'boot'} (expected file starting with 'vmlinuz')."
                )
            if not initramfs:
                raise ISOEngineError(
                    f"No initramfs found in {self.target_root / 'boot'} (expected file starting with 'initrd')."
                )

        if self.mode != "mock":
            src_kernel = self.target_root / "boot" / kernel
            src_initramfs = self.target_root / "boot" / initramfs
            if src_kernel.exists():
                shutil.copy2(src_kernel, self.iso_staging / "boot" / kernel)
            if src_initramfs.exists():
                shutil.copy2(src_initramfs, self.iso_staging / "boot" / initramfs)

        squashfs_path = self.iso_staging / "LiveOS" / "squashfs.img"
        self._create_squashfs(self.target_root, squashfs_path)

        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()

        distro_name = self.config.get("distro_name", "openSUSE Modern")
        grub_menu = (
            "set default=0\nset timeout=5\n\n"
            "insmod gzio\ninsmod part_gpt\ninsmod part_msdos\ninsmod ext2\ninsmod fat\ninsmod iso9660\ninsmod normal\n\n"
            f"search --no-floppy --set=root --file /boot/{kernel}\n\n"
            f"menuentry 'Start {distro_name}' {{\n"
            f"    search --no-floppy --set=root --file /boot/{kernel}\n"
            f"    linux /boot/{kernel} {kernel_params}\n"
            f"    initrd /boot/{initramfs}\n"
            "}\n\n"
            f"menuentry 'Start {distro_name} (Failsafe Mode)' {{\n"
            f"    search --no-floppy --set=root --file /boot/{kernel}\n"
            f"    linux /boot/{kernel} {kernel_params} nomodeset xci586 noapic acpi=off\n"
            f"    initrd /boot/{initramfs}\n"
            "}\n"
        )
        for d in [
            self.iso_staging / "boot" / "grub",
            self.iso_staging / "boot" / "grub2",
            self.iso_staging / "EFI" / "BOOT",
        ]:
            d.mkdir(parents=True, exist_ok=True)
            (d / "grub.cfg").write_text(grub_menu)

        self.generate_grub_efi_image()
        self.generate_grub_bios_core()

        iso_path = resolve_from_project(f"output/{self.output_name}.iso")
        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            iso_path.touch()
        else:
            xorriso_args = [
                "-as", "mkisofs",
                "-V", iso_label,
                "-rock", "-joliet",
            ]
            # El Torito BIOS boot image (GRUB core.img) so legacy BIOS can boot.
            grub_core = self.iso_staging / "boot" / "grub2" / "core.img"
            if grub_core.exists() and grub_core.stat().st_size > 0:
                xorriso_args += [
                    "-b", "boot/grub2/core.img",
                    "-no-emul-boot",
                    "-boot-load-size", "4",
                    "-boot-info-table",
                ]
            # El Torito UEFI boot image (efiboot.img) so UEFI firmware can boot.
            efiboot_img = self.iso_staging / "boot" / "grub2" / "efiboot.img"
            if efiboot_img.exists() and efiboot_img.stat().st_size > 0:
                xorriso_args += [
                    "-eltorito-alt-boot",
                    "-e", "boot/grub2/efiboot.img",
                    "-no-emul-boot",
                    "-isohybrid-gpt-basdat",
                    "-append_partition", "2", "0xef", str(efiboot_img),
                ]
            xorriso_args += [
                "-o", str(iso_path),
                str(self.iso_staging),
            ]
            self.toolchain.run_tool("xorriso", xorriso_args, check=True)

        if not iso_path.exists() or (self.mode != "mock" and iso_path.stat().st_size == 0):
            raise ISOEngineError(f"xorriso did not create a valid ISO: {iso_path}")

        return iso_path

    def build_tarball(self) -> Path:
        tar_path = resolve_from_project(f"output/stage3_seeds/{self.output_name}.tar.xz")
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            tar_path.touch()
        else:
            cmd = [
                "tar", "cJpf", str(tar_path),
                "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
                "--exclude=./tmp/*", "--exclude=./run/*",
                "-C", str(self.target_root), "."
            ]
            subprocess.run(cmd, check=True)
        return tar_path
