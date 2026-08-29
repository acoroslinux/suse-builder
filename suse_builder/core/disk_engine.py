import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("disk_engine")

class DiskEngineError(Exception):
    pass

class DiskEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str, toolchain: Optional[Any] = None):
        self.workdir = Path(workdir).resolve()
        self.target_root = Path(target_root).resolve()
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()
        self.toolchain = toolchain

    def _calculate_image_size(self, rootfs: Path) -> int:
        if self.mode == "mock":
            return 1024
        out = subprocess.check_output(["du", "-sm", str(rootfs)])
        return int(out.split()[0]) + 600

    def build_disk_image(self, target_format: str = "img") -> Path:
        fmt_clean = target_format.lower().lstrip(".")
        if fmt_clean in {"img", "raw"}:
            out_ext = "img"
        else:
            out_ext = fmt_clean

        out_path = resolve_from_project(f"output/{self.output_name}.img")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.mode == "mock":
            out_path.touch()
            if out_ext != "img":
                final_out = resolve_from_project(f"output/{self.output_name}.{out_ext}")
                final_out.touch()
                return final_out
            return out_path
            
        rootfs_size = self._calculate_image_size(self.target_root)
        efi_size = 300
        total_size = rootfs_size + efi_size + 4

        efi_img = self.workdir / "efi.img"
        root_img = self.workdir / "root.img"
        fs_type = self.config.get("fs_type", "btrfs")
        
        logger.info(f"Generating {fs_type.upper()} root filesystem ({rootfs_size} MB)...")
        # Build root image directly from directory
        if self.toolchain:
            self.toolchain.run_in_build_host(["truncate", "-s", f"{rootfs_size}M", str(root_img)], check=True)
            if fs_type == "btrfs":
                self.toolchain.run_in_build_host(["mkfs.btrfs", "-L", "ROOTFS", "-r", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "f2fs":
                self.toolchain.run_in_build_host(["mkfs.f2fs", "-l", "ROOTFS", str(root_img)], check=True)
                self.toolchain.run_in_build_host(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=False)
            else:
                self.toolchain.run_in_build_host(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)
        else:
            subprocess.run(["truncate", "-s", f"{rootfs_size}M", str(root_img)], check=True)
            if fs_type == "btrfs":
                subprocess.run(["mkfs.btrfs", "-L", "ROOTFS", "-r", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "f2fs":
                subprocess.run(["mkfs.f2fs", "-l", "ROOTFS", str(root_img)], check=True)
                subprocess.run(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=False)
            else:
                subprocess.run(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)

        # Update rootfs_size because mkfs.btrfs -r dynamically expands the file size!
        rootfs_size = (root_img.stat().st_size // (1024 * 1024)) + 10
        efi_size = self.config.get("bootloader", {}).get("efi_size", 300)
        total_size = rootfs_size + efi_size + 4
        
        logger.info(f"Generating FAT32 EFI filesystem ({efi_size} MB)...")
        if self.toolchain:
            self.toolchain.run_in_build_host(["truncate", "-s", f"{efi_size}M", str(efi_img)], check=True)
            self.toolchain.run_in_build_host(["mkfs.fat", "-F", "32", str(efi_img)], check=True)
        else:
            subprocess.run(["truncate", "-s", f"{efi_size}M", str(efi_img)], check=True)
            subprocess.run(["mkfs.fat", "-F", "32", str(efi_img)], check=True)

        # Handle EFI files
        efi_boot_dir = self.workdir / "efi_tmp" / "EFI" / "BOOT"
        efi_boot_dir.mkdir(parents=True, exist_ok=True)
        
        bootloader_type = self.config.get("bootloader", {}).get("type", "grub2-hybrid")
        boot_dir = self.target_root / "boot"
        vmlinuz = next((f.name for f in boot_dir.glob("vmlinuz-*") if not f.name.endswith(".old") and "rescue" not in f.name), "vmlinuz")
        initrd = next((f.name for f in boot_dir.glob("initramfs-*.img") if not f.name.endswith(".old") and not "kdump" in f.name and "rescue" not in f.name), "initrd")
        
        kernel_params = self.config.get("boot", {}).get("kernel_params", "quiet splash")
        
        if bootloader_type == "systemd-boot":
            # Install systemd-boot
            sd_boot_src = self.target_root / "usr" / "lib" / "systemd" / "boot" / "efi" / "systemd-bootx64.efi"
            if sd_boot_src.exists():
                shutil.copy2(sd_boot_src, efi_boot_dir / "BOOTX64.EFI")
            
            # Copy kernel and initrd to ESP
            if (boot_dir / vmlinuz).exists():
                shutil.copy2(boot_dir / vmlinuz, self.workdir / "efi_tmp" / vmlinuz)
            if (boot_dir / initrd).exists():
                shutil.copy2(boot_dir / initrd, self.workdir / "efi_tmp" / initrd)
            
            # Create loader/loader.conf
            loader_dir = self.workdir / "efi_tmp" / "loader"
            loader_dir.mkdir(parents=True, exist_ok=True)
            (loader_dir / "loader.conf").write_text("default suse\ntimeout 3\n")
            
            # Create loader/entries/suse.conf
            entries_dir = loader_dir / "entries"
            entries_dir.mkdir(parents=True, exist_ok=True)
            (entries_dir / "suse.conf").write_text(f"""title openSUSE
linux /{vmlinuz}
initrd /{initrd}
options root=LABEL=ROOTFS rw {kernel_params}
""")
        else:
            # SUSE GRUB
            efi_suse_src = self.target_root / "boot" / "efi" / "EFI" / "opensuse"
            if not efi_suse_src.exists():
                efi_suse_src = self.target_root / "usr" / "lib" / "grub2" / "x86_64-efi"
                
            if efi_suse_src.exists():
                shutil.copytree(efi_suse_src, self.workdir / "efi_tmp" / "EFI" / "opensuse", dirs_exist_ok=True)
                
            bootx64 = efi_boot_dir / "BOOTX64.EFI"
            if not bootx64.exists():
                shim = self.workdir / "efi_tmp" / "EFI" / "opensuse" / "shim.efi"
                grub = self.workdir / "efi_tmp" / "EFI" / "opensuse" / "grub.efi"
                if shim.exists():
                    shutil.copy2(shim, bootx64)
                elif grub.exists():
                    shutil.copy2(grub, bootx64)
            
            grub_cfg = self.workdir / "efi_tmp" / "EFI" / "opensuse" / "grub.cfg"
            grub_cfg.parent.mkdir(parents=True, exist_ok=True)
            grub_cfg.write_text(f"""
search --no-floppy --set=root --label ROOTFS
set prefix=($root)/boot/grub2
menuentry "openSUSE" {{
    linux /boot/{vmlinuz} root=LABEL=ROOTFS rw {kernel_params}
    initrd /boot/{initrd}
}}
""")

        if self.toolchain:
            self.toolchain.run_in_build_host(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/EFI", "::/"], check=True)
            if (self.workdir / "efi_tmp" / "loader").exists():
                self.toolchain.run_in_build_host(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/loader", "::/"], check=True)
            if (self.workdir / "efi_tmp" / vmlinuz).exists():
                self.toolchain.run_in_build_host(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{vmlinuz}", "::/"], check=True)
                self.toolchain.run_in_build_host(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{initrd}", "::/"], check=True)
        else:
            subprocess.run(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/EFI", "::/"], check=True)
            if (self.workdir / "efi_tmp" / "loader").exists():
                subprocess.run(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/loader", "::/"], check=True)
            if (self.workdir / "efi_tmp" / vmlinuz).exists():
                subprocess.run(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{vmlinuz}", "::/"], check=True)
                subprocess.run(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{initrd}", "::/"], check=True)

        logger.info(f"Building partitioned disk image ({total_size} MB)...")
        if self.toolchain:
            self.toolchain.run_in_build_host(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", "1MiB", f"{efi_size+1}MiB"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mkpart", "primary", fs_type, f"{efi_size+1}MiB", "100%"], check=True)
            self.toolchain.run_in_build_host(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", "seek=1", "conv=notrunc", "status=none"], check=True)
            self.toolchain.run_in_build_host(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+1}", "conv=notrunc", "status=none"], check=True)
        else:
            subprocess.run(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", "1MiB", f"{efi_size+1}MiB"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mkpart", "primary", fs_type, f"{efi_size+1}MiB", "100%"], check=True)
            subprocess.run(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", "seek=1", "conv=notrunc", "status=none"], check=True)
            subprocess.run(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+1}", "conv=notrunc", "status=none"], check=True)

        final_path = out_path
        if out_ext != "img":
            final_path = resolve_from_project(f"output/{self.output_name}.{out_ext}")
            self._convert_disk_format(out_path, final_path, out_ext)
            out_path.unlink(missing_ok=True)
            
        bootloader_type = self.config.get("bootloader", "")

        if bootloader_type.startswith("u-boot"):

            if bootloader_type == "u-boot-pinebookpro":

                logger.info("Injecting U-Boot for Pinebook Pro...")

                try:

                    if hasattr(self, "toolchain") and self.toolchain:

                        self.toolchain.run_in_build_host(["dd", f"if={self.target_root}/boot/u-boot/idbloader.img", f"of={out_path}", "bs=512", "seek=64", "conv=notrunc"], check=False)

                        self.toolchain.run_in_build_host(["dd", f"if={self.target_root}/boot/u-boot/u-boot.itb", f"of={out_path}", "bs=512", "seek=16384", "conv=notrunc"], check=False)

                    else:

                        import subprocess

                        subprocess.run(["dd", f"if={self.target_root}/boot/u-boot/idbloader.img", f"of={out_path}", "bs=512", "seek=64", "conv=notrunc"], check=False)

                        subprocess.run(["dd", f"if={self.target_root}/boot/u-boot/u-boot.itb", f"of={out_path}", "bs=512", "seek=16384", "conv=notrunc"], check=False)

                except Exception as e:

                    logger.warning(f"U-boot inject error (mock?): {e}")
        final_out = out_path
        if target_format != "img":
            vm_out = out_path.with_name(f"{self.output_name}.{target_format}")
            logger.info(f"Converting raw disk image to VM format: {target_format}...")
            if self.toolchain:
                self.toolchain.run_in_build_host(["qemu-img", "convert", "-f", "raw", "-O", target_format, str(out_path), str(vm_out)], check=True)
            else:
                subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", target_format, str(out_path), str(vm_out)], check=True)
            out_path.unlink()
            final_out = vm_out
            out_path = final_out

        compression = self.config.get("compression", "zstd")
        if compression != "none" and out_ext == "img":
            logger.info(f"Compressing disk image with {compression}...")
            if compression == "xz":
                cmd = ["xz", "-z9", "-T0", str(final_path)]
                final_path = Path(f"{final_path}.xz")
            elif compression == "gz" or compression == "gzip":
                cmd = ["gzip", "-9", str(final_path)]
                final_path = Path(f"{final_path}.gz")
            else: # zstd
                zstd_level = "-3" if self.config.get("fast_mode", False) else "-19"
                cmd = ["zstd", zstd_level, "-f", "-T0", "-q", "--rm", str(final_path)]
                final_path = Path(f"{final_path}.zst")
                
            if self.toolchain:
                self.toolchain.run_in_build_host(cmd, check=True)
            else:
                subprocess.run(cmd, check=True)

        logger.info(f"Disk image generated successfully at {final_path}")
        return final_path

    def _convert_disk_format(self, raw_path: Path, out_path: Path, target_fmt: str):
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
        subprocess.run(cmd, capture_output=True, text=True, check=True)
