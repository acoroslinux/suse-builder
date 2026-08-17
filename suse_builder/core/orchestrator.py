import os
import shutil
import subprocess
import json
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any
from suse_builder.core.chroot_manager import ChrootManager
from suse_builder.core.toolchain_manager import ToolchainManager
from suse_builder.core.zypper_manager import ZypperManager
from suse_builder.core.customizer import SystemCustomizer
from suse_builder.core.iso_engine import ISOEngine
from suse_builder.core.disk_engine import DiskEngine
from suse_builder.core.container_engine import ContainerEngine, ContainerEngineError
from suse_builder.core.config_loader import ConfigLoader
from suse_builder.core.hook_manager import HookManager
from suse_builder.core.branding_manager import BrandingManager
from suse_builder.core.path_utils import resolve_from_project, unmount_all_under
import logging

logger = logging.getLogger("orchestrator")

class BuildOrchestratorError(Exception):
    pass

class BuildOrchestrator:
    def __init__(
        self,
        arch: str = "x86_64",
        config_path: str = "configs/global_build.json",
        distro: Optional[str] = "tumbleweed",
        desktop: Optional[str] = None,
        kernel: Optional[str] = "kernel-default",
        bootloader: Optional[str] = "grub2-hybrid",
        variant: Optional[str] = "live",
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
        live_user: Optional[str] = None,
        live_groups: Optional[List[str]] = None,
        hostname: Optional[str] = None,
        output_format: str = "iso",
        compression: str = "zstd",
        mode: str = "mock",
        clean: bool = True,
        generate_manifest: bool = True,
        with_calamares: bool = False,
        multimedia_codecs: bool = False,
        with_flathub: bool = False,
        with_zram: bool = False,
        with_offline_repo: bool = False,
        offline_repo_packages: Optional[List[str]] = None,
        force_isolated_toolchain: bool = False,
    ):
        self.arch = arch
        self.config_path = config_path
        self.distro = distro
        self.desktop = desktop
        self.kernel = kernel
        self.bootloader = bootloader
        self.variant = variant
        self.package_profiles = package_profiles or []
        self.service_profiles = service_profiles or []
        self.repo_profiles = repo_profiles or []
        self.live_profile = live_profile
        self.live_user = live_user
        self.live_groups = live_groups or []
        self.hostname = hostname
        self.output_format = output_format
        self.compression = compression
        self.mode = mode.lower()
        self.clean = clean
        self.generate_manifest = generate_manifest
        self.with_calamares = with_calamares
        self.multimedia_codecs = multimedia_codecs
        self.with_flathub = with_flathub
        self.with_zram = with_zram
        self.with_offline_repo = with_offline_repo
        self.offline_repo_packages = offline_repo_packages or []
        self.force_isolated_toolchain = force_isolated_toolchain

        if self.multimedia_codecs and "multimedia" not in self.package_profiles:
            self.package_profiles.append("multimedia")
        if self.multimedia_codecs and "packman" not in self.repo_profiles:
            self.repo_profiles.append("packman")
        if self.with_offline_repo and "offline-repo" not in self.package_profiles:
            self.package_profiles.append("offline-repo")

        self.workdir = resolve_from_project(f"workdir/{self.arch}")
        self.target_root = self.workdir / "chroot"
        self.loader = ConfigLoader()

        cfg_file = resolve_from_project(self.config_path)
        self.config = self.loader.assemble_build_config(
            global_config_path=cfg_file,
            architecture=self.arch,
            distro=self.distro,
            desktop=self.desktop,
            kernel=self.kernel,
            bootloader=self.bootloader,
            variant=self.variant,
            package_profiles=self.package_profiles,
            service_profiles=self.service_profiles,
            repo_profiles=self.repo_profiles,
            live_profile=self.live_profile,
        )
        self.config["with_calamares"] = self.with_calamares
        self.config["with_flathub"] = self.with_flathub
        self.config["with_zram"] = self.with_zram
        self.config["compression"] = self.compression
        if self.hostname:
            self.config["hostname"] = self.hostname
        if self.live_user:
            live_config = dict(self.config.get("live_user", {}))
            live_config["name"] = self.live_user
            if self.live_groups:
                live_config["groups"] = self.live_groups
            self.config["live_user"] = live_config

        essential_boot_pkgs = [
            "grub2", "grub2-x86_64-efi", "grub2-i386-pc", "shim",
            "dosfstools", "mtools", "efibootmgr", "syslinux"
        ]
        
        if self.with_calamares:
            essential_boot_pkgs.extend(["calamares"])
            
        if self.with_zram:
            essential_boot_pkgs.extend(["systemd-zram-service"])
            
        for pkg in essential_boot_pkgs:
            if pkg not in self.config.get("packages", []):
                self.config.setdefault("packages", []).append(pkg)

    def validate(self) -> Dict[str, Any]:
        errors = []
        valid_formats = {"iso", "img", "qcow2", "vmdk", "vhd", "vdi", "tarball", "container"}
        if self.arch not in {"x86_64", "amd64", "i686", "i586", "aarch64", "riscv64"}:
            errors.append(f"Unsupported architecture: {self.arch}")
        if self.output_format not in valid_formats:
            errors.append(f"Unsupported output format: {self.output_format}")
        if not self.config.get("distro"):
            errors.append("Distro profile did not provide a distro identifier.")
        if not self.config.get("arch"):
            errors.append("Architecture profile did not provide an architecture identifier.")
        if self.output_format == "iso" and not self.config.get("bootloader"):
            errors.append("An ISO build requires a bootloader profile.")
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "summary": {
                "arch": self.arch,
                "distro": self.distro,
                "desktop": self.desktop or "(none)",
                "variant": self.variant or "live",
            }
        }

    def build(self, output_name: Optional[str] = None) -> Path:
        report = self.validate()
        if not report["valid"]:
            raise BuildOrchestratorError("Invalid build configuration: " + "; ".join(report["errors"]))
        if output_name:
            name = output_name
        else:
            desktop_part = f"-{self.desktop}" if self.desktop else ""
            kernel_part = f"-{self.kernel}" if self.kernel else ""
            name = f"suse-{self.distro}{desktop_part}{kernel_part}-{self.arch}"

        current_fingerprint = self._effective_build_fingerprint()
        previous_fingerprint = self._load_previous_build_fingerprint()
        reuse_existing_rootfs = (
            self.mode != "mock"
            and not self.clean
            and self.target_root.exists()
            and previous_fingerprint is not None
            and previous_fingerprint == current_fingerprint
        )

        if self.mode != "mock" and not self.clean and previous_fingerprint and previous_fingerprint != current_fingerprint:
            logger.info("Build choices changed since previous run; forcing rootfs rebuild despite --no-clean.")

        if self.clean and self.mode != "mock":
            if os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))
            if self.workdir.exists():
                shutil.rmtree(self.workdir, ignore_errors=True)

        toolchain = ToolchainManager(
            workdir_base=self.workdir,
            mode=self.mode,
            force_isolated=self.force_isolated_toolchain,
            target_arch=self.arch,
            distro=self.distro,
        )
        toolchain.setup()

        chroot = ChrootManager(self.target_root, self.mode, cache_dir=resolve_from_project(f"cache/{self.arch}"), arch=self.arch)
        hook_manager = HookManager(chroot, self.config)
        
        try:
            hook_manager.run_stage("pre-chroot")
            
            toolchain.mount_virtual_fs()
            chroot.mount_virtual_fs()

            zypper = ZypperManager(chroot, self.config, toolchain=toolchain)
            zypper.bootstrap_rootfs(self.distro, self.arch, reuse_existing=reuse_existing_rootfs)
            zypper.add_repositories()
            zypper.refresh()

            chroot.prepare_emulation()

            pkgs = self.config.get("packages", [])
            zypper.install_packages(pkgs)

            # Prepare offline package repository if requested
            offline_pkgs = list(self.config.get("offline_repo_packages", []))
            if self.offline_repo_packages:
                for p in self.offline_repo_packages:
                    if p not in offline_pkgs:
                        offline_pkgs.append(p)

            if (self.with_offline_repo or offline_pkgs) and self.output_format == "iso":
                offline_repo_dir = self.workdir / "offline_repo" / self.arch
                zypper.download_offline_packages(offline_pkgs, offline_repo_dir)
                self.config["offline_repo_dir"] = str(offline_repo_dir)
                self.config["with_offline_repo"] = True

            customizer = SystemCustomizer(chroot, self.config)
            customizer.configure_live_environment()

            # Apply visual branding
            branding = BrandingManager(chroot, self.config)
            branding.apply_branding()

            # Run in-chroot hooks after all system configuration is done
            hook_manager.run_stage("chroot")

            self._ensure_iso_boot_artifacts(chroot)

            chroot.umount_virtual_fs()

            # Run post-chroot hooks before ISO/Image generation
            hook_manager.run_stage("post-chroot")

            iso_engine = ISOEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain)
            if self.output_format in {"img", "qcow2", "vmdk", "vhd", "vdi"}:
                disk_engine = DiskEngine(self.workdir, self.target_root, name, self.config, self.mode)
                artifact = disk_engine.build_disk_image(target_format=self.output_format)
            elif self.output_format == "tarball":
                artifact = iso_engine.build_tarball()
            elif self.output_format in {"container", "oci"}:
                container_engine = ContainerEngine(self.target_root, name, self.config, self.mode)
                artifact = container_engine.build_oci_archive()
            else:
                artifact = iso_engine.build_iso()

            if not artifact.exists():
                raise BuildOrchestratorError(f"Build did not produce the expected artifact: {artifact}")

            if self.generate_manifest and artifact and artifact.exists():
                self._generate_checksums(artifact)

            if self.mode != "mock":
                self._save_build_fingerprint(current_fingerprint)

            output_dir = resolve_from_project("output")
            self._fix_output_permissions(output_dir)

            return artifact
        finally:
            try:
                chroot.umount_virtual_fs()
            except Exception:
                pass
            try:
                toolchain.umount_virtual_fs()
            except Exception:
                pass

            if self.mode != "mock" and os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))

            output_dir = resolve_from_project("output")
            self._fix_output_permissions(output_dir)

    def _ensure_iso_boot_artifacts(self, chroot: ChrootManager) -> None:
        """Make sure kernel + initramfs exist before packaging an ISO."""
        if self.mode == "mock" or self.output_format != "iso":
            return

        boot_dir = self.target_root / "boot"
        if not boot_dir.exists():
            raise BuildOrchestratorError(f"Missing boot directory in rootfs: {boot_dir}")

        kernels = sorted(
            f.name for f in boot_dir.iterdir() if f.is_file() and f.name.startswith("vmlinuz")
        )
        initrds = sorted(
            f.name for f in boot_dir.iterdir() if f.is_file() and f.name.startswith("initrd")
        )

        if not kernels:
            raise BuildOrchestratorError(
                f"No kernel found in {boot_dir}; expected a file starting with 'vmlinuz'."
            )

        logger.info("Generating live-capable initramfs using dracut...")
        dracut_cmd = r'''
set -eu
if ! command -v dracut >/dev/null 2>&1; then
    echo "dracut is not installed in the target rootfs" >&2
    exit 1
fi
found_kernel=0
for kimg in /boot/vmlinuz-*; do
    [ -e "$kimg" ] || continue
    found_kernel=1
    kver="${kimg#/boot/vmlinuz-}"
    dracut --force --no-hostonly \
      --kver "$kver" \
      --add "dmsquash-live pollcdrom qemu qemu-net base rootfs-block udev-rules kernel-modules" \
      --add-drivers "squashfs loop overlay iso9660 isofs zstd zstd_decompress dm_mod sr_mod cdrom sd_mod ahci ata_piix ata_generic pata_acpi pata_serverworks virtio_blk virtio_scsi virtio_pci virtio_net uas usb_storage nvme" \
      --filesystems "squashfs iso9660 overlay vfat ext4" \
      --include /etc/systemd/system/checkisomd5@.service.d /etc/systemd/system/checkisomd5@.service.d \
      "/boot/initrd-$kver"
done
if [ "$found_kernel" -eq 0 ]; then
    echo "No /boot/vmlinuz-* kernels found for dracut" >&2
    exit 1
fi
'''
        chroot.run_in_chroot(["/bin/sh", "-lc", dracut_cmd], check=True)

        initrds = sorted(
            f.name for f in boot_dir.iterdir() if f.is_file() and f.name.startswith("initrd")
        )
        if not initrds:
            raise BuildOrchestratorError(
                "dracut completed but no initramfs was produced under /boot. "
                "Check kernel modules and dracut configuration in the target rootfs."
            )

    def _fix_output_permissions(self, output_dir: Path):
        """Fix ownership of output directory and workdir from root to SUDO_USER if invoked via sudo."""
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if not (sudo_uid and sudo_gid):
            return

        try:
            uid = int(sudo_uid)
            gid = int(sudo_gid)
            targets = [output_dir, self.workdir]
            for target_path in targets:
                if not target_path.exists():
                    continue
                for root, dirs, files in os.walk(target_path):
                    for d in dirs:
                        try:
                            os.chown(os.path.join(root, d), uid, gid)
                        except Exception:
                            pass
                    for f in files:
                        try:
                            os.chown(os.path.join(root, f), uid, gid)
                        except Exception:
                            pass
                os.chown(target_path, uid, gid)
            logger.info(f"Updated ownership of build artifacts and workdir to non-root user ({sudo_uid}:{sudo_gid})")
        except Exception as e:
            logger.warning(f"Could not update output ownership: {e}")

    def _generate_checksums(self, artifact_path: Path):
        if not artifact_path or not artifact_path.exists():
            return
        import hashlib
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        with open(artifact_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
                md5.update(chunk)

        sha256_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
        md5_path = artifact_path.with_name(f"{artifact_path.name}.md5")
        sha256_path.write_text(f"{sha256.hexdigest()}  {artifact_path.name}\n")
        md5_path.write_text(f"{md5.hexdigest()}  {artifact_path.name}\n")

        # Generate package manifest file
        manifest_path = artifact_path.with_name(f"{artifact_path.name}.manifest")
        pkgs = sorted(self.config.get("packages", []))
        manifest_lines = [f"# SUSE-Builder Package Manifest for {artifact_path.name}\n"]
        manifest_lines.extend(f"{pkg}\n" for pkg in pkgs)
        manifest_path.write_text("".join(manifest_lines))

    def _state_file_path(self) -> Path:
        return self.workdir / ".build_state.json"

    def _effective_build_fingerprint(self) -> str:
        # Keep only rootfs-affecting choices so no-clean reuse remains safe.
        payload = {
            "arch": self.arch,
            "distro": self.distro,
            "desktop": self.desktop,
            "kernel": self.kernel,
            "bootloader": self.bootloader,
            "variant": self.variant,
            "package_profiles": sorted(self.package_profiles),
            "service_profiles": sorted(self.service_profiles),
            "repo_profiles": sorted(self.repo_profiles),
            "live_profile": self.live_profile,
            "live_user": self.live_user,
            "live_groups": sorted(self.live_groups),
            "with_calamares": self.with_calamares,
            "multimedia_codecs": self.multimedia_codecs,
            "with_flathub": self.with_flathub,
            "with_zram": self.with_zram,
            "config": self.config,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _load_previous_build_fingerprint(self) -> Optional[str]:
        state_file = self._state_file_path()
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text())
            value = data.get("fingerprint")
            return value if isinstance(value, str) and value else None
        except Exception:
            return None

    def _save_build_fingerprint(self, fingerprint: str) -> None:
        state_file = self._state_file_path()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"fingerprint": fingerprint}, indent=2, sort_keys=True))
