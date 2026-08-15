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
        default_params = f"root=live:CDLABEL={iso_label} rd.live.image rd.live.dir=LiveOS rd.live.squashimg=squashfs.img quiet splash"
        return self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", default_params))

    def _get_template_placeholders(self) -> Dict[str, str]:
        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()
        desktop = str(self.config.get("desktop", "xfce")).upper()
        distro = str(self.config.get("distro", "openSUSE")).title()
        arch = self.arch
        keymap = self.config.get("keymap", "us")
        locale = self.config.get("locale", "en_US.UTF-8")
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        return {
            "@@VOL_ID@@": iso_label,
            "@@ISO_LABEL@@": iso_label,
            "@@BOOT_TITLE@@": f"{distro} Modern",
            "@@DISTRO_NAME@@": f"{distro} Modern",
            "@@DESKTOP@@": desktop,
            "@@ARCH@@": arch,
            "@@KERNEL_PARAMS@@": kernel_params,
            "@@BOOT_CMDLINE@@": kernel_params,
            "@@KEYMAP@@": keymap,
            "@@LOCALE@@": locale,
            "@@LIVE_USER@@": live_user,
            "@@SPLASHIMAGE@@": "splash.png"
        }

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
            "insmod fat\n"
            "insmod part_gpt\n"
            "insmod part_msdos\n"
            "insmod test\n"
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

        efi_modules = "iso9660 search search_fs_file search_label configfile normal linux gzio part_gpt part_msdos fat ext2 test echo loadenv all_video gfxterm font gettext png terminal"

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
                    f"--install-modules={efi_modules}",
                    f"--modules={efi_modules}",
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
        """Build GRUB BIOS eltorito image (cdboot.img + core.img) for legacy BIOS boot."""
        eltorito_img = self.iso_staging / "boot" / "grub2" / "eltorito.img"
        eltorito_img.parent.mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub").mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            eltorito_img.touch()
            (self.iso_staging / "boot" / "grub2" / "core.img").touch()
            return

        # 1. Search for i386-pc directory with cdboot.img
        i386_dir = None
        for candidate in [
            self.target_root / "usr" / "lib" / "grub2" / "i386-pc",
            self.target_root / "usr" / "share" / "grub2" / "i386-pc",
            self.target_root / "usr" / "lib" / "grub" / "i386-pc",
            self.workdir / "build_host" / "usr" / "lib" / "grub2" / "i386-pc",
            self.workdir / "build_host" / "usr" / "lib" / "grub" / "i386-pc",
            Path("/usr/lib/grub2/i386-pc"),
            Path("/usr/lib/grub/i386-pc"),
            Path("/usr/share/grub2/i386-pc"),
        ]:
            if candidate.exists() and (candidate / "cdboot.img").exists():
                i386_dir = candidate
                break

        if i386_dir:
            # Copy all GRUB modules to /boot/grub2/i386-pc and /boot/grub/i386-pc
            for target_d in [self.iso_staging / "boot" / "grub2" / "i386-pc", self.iso_staging / "boot" / "grub" / "i386-pc"]:
                target_d.mkdir(parents=True, exist_ok=True)
                for item in i386_dir.glob("*"):
                    if item.is_file() and item.suffix in [".mod", ".lst", ".pf2"]:
                        shutil.copy2(item, target_d / item.name)

            early_cfg = self.workdir / "early-bios-grub.cfg"
            early_cfg.write_text(
                "insmod search\n"
                "insmod search_fs_file\n"
                "insmod search_label\n"
                "insmod iso9660\n"
                "insmod fat\n"
                "insmod part_gpt\n"
                "insmod part_msdos\n"
                "insmod test\n"
                f"search --no-floppy --set=root --label {self._get_iso_label()}\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /boot/mbrid\n"
                "fi\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /boot/vmlinuz\n"
                "fi\n"
                "set prefix=($root)/boot/grub2\n"
                "if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
                "    configfile ($root)/boot/grub2/grub.cfg\n"
                "elif [ -f ($root)/boot/grub/grub.cfg ]; then\n"
                "    configfile ($root)/boot/grub/grub.cfg\n"
                "fi\n"
            )

            core_tmp = self.workdir / "core_tmp.img"
            if core_tmp.exists():
                core_tmp.unlink()

            grub_mkimage = "grub2-mkimage" if (self.toolchain.use_isolated or shutil.which("grub2-mkimage")) else ("grub-mkimage" if shutil.which("grub-mkimage") else None)
            if grub_mkimage:
                res = self.toolchain.run_tool(
                    grub_mkimage,
                    [
                        "-d", str(i386_dir),
                        "-c", str(early_cfg),
                        "-o", str(core_tmp),
                        "-O", "i386-pc",
                        "--prefix=/boot/grub2",
                        "biosdisk", "iso9660", "search", "search_fs_file", "search_label", "configfile", "normal", "linux", "gzio", "part_gpt", "part_msdos", "fat", "ext2", "test", "echo", "loadenv", "all_video", "gfxterm", "font", "gettext", "png", "terminal"
                    ],
                    check=False
                )
                if res.returncode == 0 and core_tmp.exists() and core_tmp.stat().st_size > 0:
                    cdboot_path = i386_dir / "cdboot.img"
                    with open(eltorito_img, "wb") as f_out:
                        f_out.write(cdboot_path.read_bytes())
                        f_out.write(core_tmp.read_bytes())
                    shutil.copy2(eltorito_img, self.iso_staging / "boot" / "grub" / "eltorito.img")
                    shutil.copy2(core_tmp, self.iso_staging / "boot" / "grub2" / "core.img")
                    logger.info(f"Successfully generated GRUB BIOS El Torito image: {eltorito_img}")
                    return

        # 2. Fallback: Standalone GRUB BIOS image via grub-mkstandalone
        grub_mk = "grub2-mkstandalone" if (self.toolchain.use_isolated or shutil.which("grub2-mkstandalone")) else ("grub-mkstandalone" if shutil.which("grub-mkstandalone") else None)
        if grub_mk:
            iso_label = self._get_iso_label()
            embed_cfg = self.workdir / "tmp_bios_grub.cfg"
            embed_cfg.write_text(
                "insmod search\n"
                "insmod search_fs_file\n"
                "insmod search_label\n"
                "insmod iso9660\n"
                "insmod configfile\n"
                "search --no-floppy --set=root --file /boot/vmlinuz\n"
                "set prefix=($root)/boot/grub2\n"
                "if [ -f ($root)/boot/grub2/grub.cfg ]; then\n"
                "    configfile ($root)/boot/grub2/grub.cfg\n"
                "fi\n"
            )
            res = self.toolchain.run_tool(
                grub_mk,
                [
                    "--format=i386-pc",
                    "--fonts=",
                    "--locales=",
                    "--themes=",
                    "--install-modules=iso9660 search search_fs_file search_label configfile normal biosdisk part_msdos part_gpt linux fat ext2 test",
                    "-o", str(eltorito_img),
                    f"boot/grub/grub.cfg={embed_cfg}",
                ],
                check=False,
            )
            if res.returncode != 0 or not eltorito_img.exists() or eltorito_img.stat().st_size == 0:
                eltorito_img.unlink(missing_ok=True)
                (self.iso_staging / "boot" / "grub" / "eltorito.img").unlink(missing_ok=True)
                (self.iso_staging / "boot" / "grub2" / "core.img").unlink(missing_ok=True)
            else:
                shutil.copy2(eltorito_img, self.iso_staging / "boot" / "grub" / "eltorito.img")
                shutil.copy2(eltorito_img, self.iso_staging / "boot" / "grub2" / "core.img")

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
                shutil.copy2(src_kernel, self.iso_staging / "boot" / "vmlinuz")
            if src_initramfs.exists():
                shutil.copy2(src_initramfs, self.iso_staging / "boot" / initramfs)
                shutil.copy2(src_initramfs, self.iso_staging / "boot" / "initrd")

        # Copy GRUB x86_64-efi modules to ISO
        for mod_candidate in [
            self.target_root / "usr" / "lib" / "grub2" / "x86_64-efi",
            self.target_root / "usr" / "share" / "grub2" / "x86_64-efi",
            self.target_root / "usr" / "lib" / "grub" / "x86_64-efi",
            self.workdir / "build_host" / "usr" / "lib" / "grub2" / "x86_64-efi",
            Path("/usr/lib/grub2/x86_64-efi"),
            Path("/usr/lib/grub/x86_64-efi"),
        ]:
            if mod_candidate.exists():
                for d in [self.iso_staging / "boot" / "grub2" / "x86_64-efi", self.iso_staging / "boot" / "grub" / "x86_64-efi"]:
                    d.mkdir(parents=True, exist_ok=True)
                    for mod_f in mod_candidate.glob("*"):
                        if mod_f.is_file() and mod_f.suffix in [".mod", ".lst"]:
                            shutil.copy2(mod_f, d / mod_f.name)
                break

        squashfs_path = self.iso_staging / "LiveOS" / "squashfs.img"
        self._create_squashfs(self.target_root, squashfs_path)

        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()
        placeholders = self._get_template_placeholders()

        # 1. Load config.cfg from template if available
        config_template = resolve_from_project("configs/bootloaders/templates/config.cfg.in")
        if config_template.exists():
            config_cfg_text = config_template.read_text()
            for k, v in placeholders.items():
                config_cfg_text = config_cfg_text.replace(k, str(v))
        else:
            config_cfg_text = (
                "set default=0\n\n"
                "if [ -e \"${prefix}/${grub_cpu}-${grub_platform}/all_video.mod\" ]; then\n"
                "    insmod all_video\n"
                "else\n"
                "    insmod efi_gop\n"
                "    insmod efi_uga\n"
                "    insmod video_bochs\n"
                "    insmod video_cirrus\n"
                "fi\n\n"
                "insmod font\n"
                "insmod png\n"
                "insmod part_gpt\n"
                "insmod part_msdos\n"
                "insmod fat\n"
                "insmod iso9660\n"
                "insmod ext2\n\n"
                "if loadfont /boot/grub2/unicode.pf2 ; then\n"
                "    set gfxmode=auto\n"
                "    insmod gfxterm\n"
                "    terminal_output gfxterm\n"
                "fi\n"
            )

        # 2. Load grub.cfg from template if available
        grub_template = resolve_from_project("configs/bootloaders/templates/grub.cfg.in")
        if grub_template.exists():
            grub_menu = grub_template.read_text()
            for k, v in placeholders.items():
                grub_menu = grub_menu.replace(k, str(v))
        else:
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

        # 3. Load loopback.cfg from template if available
        loopback_template = resolve_from_project("configs/bootloaders/templates/loopback.cfg.in")
        if loopback_template.exists():
            loopback_cfg_text = loopback_template.read_text()
            for k, v in placeholders.items():
                loopback_cfg_text = loopback_cfg_text.replace(k, str(v))
        else:
            loopback_cfg_text = "source /boot/grub2/grub.cfg\n"

        for d in [
            self.iso_staging / "boot" / "grub",
            self.iso_staging / "boot" / "grub2",
            self.iso_staging / "EFI" / "BOOT",
        ]:
            d.mkdir(parents=True, exist_ok=True)
            (d / "config.cfg").write_text(config_cfg_text)
            (d / "grub.cfg").write_text(grub_menu)
            (d / "loopback.cfg").write_text(loopback_cfg_text)

        # Copy unicode.pf2 font if available
        for font_candidate in [
            self.target_root / "usr" / "share" / "grub2" / "unicode.pf2",
            self.target_root / "usr" / "share" / "grub" / "unicode.pf2",
            self.workdir / "build_host" / "usr" / "share" / "grub2" / "unicode.pf2",
            Path("/usr/share/grub2/unicode.pf2"),
            Path("/usr/share/grub/unicode.pf2"),
        ]:
            if font_candidate.exists():
                shutil.copy2(font_candidate, self.iso_staging / "boot" / "grub2" / "unicode.pf2")
                shutil.copy2(font_candidate, self.iso_staging / "boot" / "grub" / "unicode.pf2")
                break

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
                "-joliet-long",
                "-cache-inodes",
            ]

            # Locate MBR file if available
            mbr_bin = None
            for candidate in [
                self.target_root / "usr" / "lib" / "grub2" / "i386-pc" / "boot_hybrid.img",
                self.target_root / "usr" / "lib" / "grub" / "i386-pc" / "boot_hybrid.img",
                self.target_root / "usr" / "lib" / "ISOLINUX" / "isohdpfx.bin",
                self.target_root / "usr" / "lib" / "syslinux" / "isohdpfx.bin",
                Path("/usr/lib/grub2/i386-pc/boot_hybrid.img"),
                Path("/usr/lib/grub/i386-pc/boot_hybrid.img"),
                Path("/usr/lib/ISOLINUX/isohdpfx.bin"),
                Path("/usr/lib/syslinux/isohdpfx.bin"),
            ]:
                if candidate.exists():
                    mbr_bin = candidate
                    break

            # BIOS boot image (eltorito.img or core.img)
            eltorito_img = self.iso_staging / "boot" / "grub2" / "eltorito.img"
            if not eltorito_img.exists():
                eltorito_img = self.iso_staging / "boot" / "grub" / "eltorito.img"
            if not eltorito_img.exists():
                eltorito_img = self.iso_staging / "boot" / "grub2" / "core.img"

            if self.should_use_grub_bios() and eltorito_img.exists() and eltorito_img.stat().st_size > 0:
                if mbr_bin:
                    if "boot_hybrid.img" in str(mbr_bin):
                        xorriso_args.extend(["--grub2-boot-info", "--grub2-mbr", str(mbr_bin)])
                    else:
                        xorriso_args.extend(["-isohybrid-mbr", str(mbr_bin)])
                xorriso_args += [
                    "-b", str(eltorito_img.relative_to(self.iso_staging)),
                    "-no-emul-boot",
                    "-boot-load-size", "4",
                    "-boot-info-table",
                ]

            # El Torito UEFI boot image (efiboot.img)
            efiboot_img = self.iso_staging / "boot" / "grub2" / "efiboot.img"
            if not efiboot_img.exists():
                efiboot_img = self.iso_staging / "boot" / "grub" / "efiboot.img"
            if self.should_use_grub_efi() and efiboot_img.exists() and efiboot_img.stat().st_size > 0:
                xorriso_args += [
                    "-eltorito-alt-boot",
                    "-e", str(efiboot_img.relative_to(self.iso_staging)),
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
