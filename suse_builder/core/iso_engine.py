import os
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
        self.mode = mode
        self.toolchain = toolchain
        self.iso_staging = self.workdir / "iso_root"
        self.arch = config.get("architecture", "x86_64")

    def _get_iso_label(self) -> str:
        return self.config.get("iso_label", self.config.get("system", {}).get("iso_label", "OPENSUSE_MODERN"))

    def _get_kernel_params(self) -> str:
        return self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", "root=live:CDLABEL=OPENSUSE_MODERN rd.live.image quiet splash"))

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
            "search --no-floppy --set=root --file /boot/mbrid\n"
            "set prefix=($root)/boot/grub2\n"
            "if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "    source ($root)/boot/grub2/grub.cfg\n"
            "fi\n"
            f"search --no-floppy --set=root --label {iso_label}\n"
            "if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
            "    source ($root)/boot/grub2/grub.cfg\n"
            "fi\n"
            "search --no-floppy --set=root --file /boot/grub2/grub.cfg\n"
            "source ($root)/boot/grub2/grub.cfg\n"
        )

        created_binaries = []
        for fmt, boot_filename in formats_to_build:
            out_binary = efi_tmp / boot_filename
            built = False
            grub_mk = "grub2-mkstandalone" if (self.toolchain.use_isolated or shutil.which("grub2-mkstandalone")) else ("grub-mkstandalone" if shutil.which("grub-mkstandalone") else None)
            if grub_mk:
                res = self.toolchain.run_tool(grub_mk, [
                    f"--format={fmt}",
                    "-o", str(out_binary), f"boot/grub/grub.cfg={embed_cfg}"
                ], check=False)
                if res.returncode == 0 and out_binary.exists():
                    built = True

            if built or out_binary.exists():
                created_binaries.append((out_binary, boot_filename))

        if created_binaries:
            iso_efi_dir = self.iso_staging / "EFI" / "BOOT"
            iso_efi_dir.mkdir(parents=True, exist_ok=True)
            for binary_path, filename in created_binaries:
                shutil.copy2(binary_path, iso_efi_dir / filename)

            self.toolchain.run_tool("truncate", ["-s", "32M", str(efiboot_img)], check=True)
            if self.toolchain.use_isolated or (shutil.which("mformat") and shutil.which("mcopy")):
                self.toolchain.run_tool("mformat", ["-i", str(efiboot_img), "-h", "32", "-t", "32", "-n", "64", "-c", "1", "::"], check=True)
                self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI"], check=True)
                self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI/BOOT"], check=True)
                for binary_path, filename in created_binaries:
                    self.toolchain.run_tool("mcopy", ["-i", str(efiboot_img), str(binary_path), f"::/EFI/BOOT/{filename}"], check=True)

        if not efiboot_img.exists():
            raise ISOEngineError("Could not create an EFI boot image; install GRUB and mtools support for the target architecture.")

        shutil.rmtree(efi_tmp, ignore_errors=True)

    def build_iso(self) -> Path:
        self.iso_staging.mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "LiveOS").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub2").mkdir(parents=True, exist_ok=True)

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

        with open(self.iso_staging / "boot" / "grub2" / "grub.cfg", "w") as f:
            f.write(
                f"set default=0\nset timeout=5\n\n"
                f"menuentry 'Start openSUSE Modern' {{\n"
                f"    linux /boot/{kernel} {kernel_params}\n"
                f"    initrd /boot/{initramfs}\n"
                f"}}\n"
            )

        self.generate_grub_efi_image()

        iso_path = resolve_from_project(f"output/{self.output_name}.iso")
        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            iso_path.touch()
        else:
            self.toolchain.run_tool(
                "xorriso",
                [
                    "-as", "mkisofs",
                    "-V", iso_label,
                    "-rock", "-joliet",
                    "-eltorito-alt-boot",
                    "-e", "boot/grub2/efiboot.img",
                    "-no-emul-boot", "-isohybrid-gpt-basdat",
                    "-o", str(iso_path),
                    str(self.iso_staging),
                ],
                check=True
            )

        if not iso_path.exists() or (self.mode != "mock" and iso_path.stat().st_size == 0):
            raise ISOEngineError(f"xorriso did not create a valid ISO: {iso_path}")

        return iso_path

    def build_tarball(self) -> Path:
        tar_path = resolve_from_project(f"output/stage3_seeds/{self.output_name}.tar.xz")
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            tar_path.touch()
        else:
            subprocess.run(["tar", "cJpf", str(tar_path), "-C", str(self.target_root), "."], check=True)
        return tar_path
