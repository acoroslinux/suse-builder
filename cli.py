#!/usr/bin/env python3
"""
cli.py — SUSE-Builder Entry Point

Modular openSUSE Linux ISO & Image Builder.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from suse_builder.core.orchestrator import BuildOrchestrator, BuildOrchestratorError
from suse_builder.core.config_loader import ConfigLoaderError
from suse_builder.core.toolchain_manager import ToolchainManagerError
from suse_builder.core.zypper_manager import ZypperManagerError
from suse_builder.core.iso_engine import ISOEngineError
from suse_builder.core.disk_engine import DiskEngineError
from suse_builder.core.container_engine import ContainerEngineError
from suse_builder.core.stage_manager import StageManager, StageManagerError
from suse_builder.core.verifier import ImageVerifier
from suse_builder.core.path_utils import resolve_from_project


def _available_profiles(config_root: Path, category: str):
    category_dir = config_root / category
    if not category_dir.exists() or not category_dir.is_dir():
        return []
    return sorted([p.stem for p in category_dir.glob("*.json")])


def _slugify_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower())
    normalized = normalized.strip("-._")
    return normalized or fallback


def _parse_list_arg(arg_value) -> list:
    if not arg_value:
        return []
    items = []
    if isinstance(arg_value, list):
        for val in arg_value:
            if isinstance(val, list):
                for inner in val:
                    items.extend([x.strip() for x in inner.split(",") if x.strip()])
            elif isinstance(val, str):
                items.extend([x.strip() for x in val.split(",") if x.strip()])
    elif isinstance(arg_value, str):
        items.extend([x.strip() for x in arg_value.split(",") if x.strip()])
    return items


VALID_ARCHS = ("x86_64", "amd64", "i686", "i586", "aarch64", "riscv64")


class CustomArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(f"❌ Error: {message}", file=sys.stderr)
        sys.exit(2)


def main():
    default_config_path = resolve_from_project("configs/global_build.json")

    parser = CustomArgumentParser(
        description="SUSE-Builder: Modular openSUSE Linux ISO & Image Builder",
        epilog="Use --help to see a detailed list of available arguments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "architecture",
        nargs="?",
        default="x86_64",
        help="Target architecture (x86_64, i586, aarch64, riscv64). Default: x86_64",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=str(default_config_path),
        help="Path to the global configuration JSON file.",
    )

    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="mock",
        help="Execution mode: 'mock' (simulation, no root required) or 'real' (actual build, requires root). Default: mock",
    )

    parser.add_argument(
        "--clean",
        dest="clean",
        action="store_true",
        default=True,
        help="Remove the target architecture work directory before a real build (default).",
    )
    parser.add_argument(
        "--no-clean",
        dest="clean",
        action="store_false",
        help="Reuse the existing real-build work directory.",
    )
    parser.add_argument(
        "--force-isolated-toolchain",
        action="store_true",
        help="Force use of the isolated openSUSE secondary build-host chroot environment.",
    )

    parser.add_argument(
        "--distro",
        type=str,
        default="leap-15.6",
        help="Distro profile (leap-15.6, tumbleweed, leap-16.0, slowroll). Default: leap-15.6",
    )

    parser.add_argument(
        "--variant",
        type=str,
        default="live",
        help="Variant profile (live, minimal). Default: live",
    )

    parser.add_argument(
        "-d",
        "--desktop",
        type=str,
        default=None,
        help="Desktop environment profile (kde, gnome, xfce, mate, lxqt, sway, hyprland).",
    )

    parser.add_argument(
        "-k",
        "--kernel",
        type=str,
        default="generic",
        help="Kernel flavor profile: 'generic' (kernel-default), 'rt' (kernel-rt), or 'cloud' (kernel-kvm). Default: generic",
    )

    parser.add_argument(
        "-b",
        "--bootloader",
        type=str,
        default="grub2-hybrid",
        help="Bootloader profile (grub2-hybrid, grub2-uefi, grub2-bios). Default: grub2-hybrid",
    )

    parser.add_argument(
        "-p",
        "--package-profile",
        action="append",
        default=[],
        help="Add package profile from configs/packages/.",
    )

    parser.add_argument(
        "-f",
        "--format",
        choices=["iso", "img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi", "tarball", "container", "oci"],
        default="iso",
        help="Output artifact format: iso, img, raw, qcow2, vmdk, vhd, vhdx, vdi, tarball, container, oci. Default: iso",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output filename for the final build artifact.",
    )

    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="Hostname for the built system (default: opensuse-modern).",
    )

    parser.add_argument(
        "--live-user",
        type=str,
        default=None,
        help="Default live environment username (default: liveuser).",
    )

    parser.add_argument(
        "--compression",
        type=str,
        default="zstd",
        help="SquashFS image compression algorithm (zstd, xz, gzip). Default: zstd",
    )

    parser.add_argument(
        "--with-calamares",
        action="store_true",
        help="Include Calamares graphical installer on the ISO.",
    )

    parser.add_argument(
        "--multimedia-codecs",
        action="store_true",
        help="Automatically install Packman multimedia codecs (H.264/AAC/VLC/FFmpeg).",
    )

    parser.add_argument(
        "--with-flathub",
        action="store_true",
        help="Configure Flathub Flatpak repository on first boot.",
    )

    parser.add_argument(
        "--with-zram",
        action="store_true",
        help="Configure systemd-zram-generator for RAM compressed swap.",
    )

    parser.add_argument(
        "--with-offline-repo",
        action="store_true",
        help="Embed an offline RPM package repository on the ISO.",
    )

    parser.add_argument(
        "--offline-repo-packages",
        type=str,
        default=None,
        help="Comma-separated list of packages to include in the offline ISO repository.",
    )

    parser.add_argument(
        "--use-tarball",
        type=str,
        default=None,
        help="Use pre-built base stage tarball (path, URL, or 'auto') to accelerate bootstrap.",
    )

    parser.add_argument(
        "--create-tarball",
        action="store_true",
        help="Package base bootstrapped system into a reusable stage tarball seed.",
    )

    parser.add_argument(
        "--verify",
        type=str,
        nargs="?",
        const="auto",
        default=None,
        help="Verify build artifact integrity and static platform correctness (or supply specific file).",
    )

    parser.add_argument(
        "--tmpfs",
        action="store_true",
        help="Build entirely inside RAM (tmpfs) to maximize I/O throughput (3x-5x faster) and avoid SSD wear.",
    )

    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast development build mode (ultra-fast ZSTD level 3 SquashFS compression).",
    )

    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Record and display a detailed execution timing benchmark report for each build stage.",
    )

    parser.add_argument(
        "--disk-size",
        type=str,
        default=None,
        help="Virtual disk or image size (e.g. 4G, 8G, 16G, 32G). Default: 8G",
    )

    parser.add_argument(
        "--filesystem",
        type=str,
        choices=["ext4", "btrfs"],
        default="ext4",
        help="Root filesystem type for disk/VM images (ext4, btrfs). Default: ext4",
    )

    parser.add_argument(
        "--list-options",
        action="store_true",
        help="List all available configuration profiles and exit.",
    )

    parser.add_argument(
        "--validate",
        dest="validate_only",
        action="store_true",
        help="Validate build configuration without performing full build.",
    )

    args = parser.parse_args()

    if args.verify and args.verify != "auto":
        verify_path = Path(args.verify)
        if not verify_path.exists():
            print(f"❌ Error: File to verify does not exist: {verify_path}", file=sys.stderr)
            sys.exit(1)
        report = ImageVerifier.verify_target(verify_path)
        report.print_summary()
        sys.exit(0 if report.all_passed else 1)

    config_root = resolve_from_project("configs")
    if args.list_options:
        print("Available SUSE-Builder profiles:")
        categories = [
            ("architectures", "architectures"),
            ("distros",       "distros      "),
            ("desktops",      "desktops     "),
            ("kernels",       "kernels      "),
            ("bootloaders",   "bootloaders  "),
            ("packages",      "packages     "),
            ("services",      "services     "),
            ("repos",         "repos        "),
        ]
        for dir_name, label in categories:
            profs = _available_profiles(config_root, dir_name)
            print(f"  {label}: {', '.join(profs) if profs else '(none)'}")
        sys.exit(0)

    arch_lower = args.architecture.lower()
    if arch_lower not in VALID_ARCHS:
        print(f"Error: Architecture '{args.architecture}' is not supported.")
        sys.exit(1)

    parsed_package_profiles = _parse_list_arg(args.package_profile)
    parsed_offline_packages = _parse_list_arg(args.offline_repo_packages)

    try:
        orchestrator = BuildOrchestrator(
            arch=arch_lower,
            config_path=args.config,
            mode=args.mode,
            clean=args.clean,
            distro=args.distro,
            desktop=args.desktop,
            kernel=args.kernel,
            bootloader=args.bootloader,
            variant=args.variant,
            package_profiles=parsed_package_profiles,
            output_format=args.format,
            compression=args.compression,
            live_user=args.live_user,
            hostname=args.hostname,
            with_calamares=args.with_calamares,
            multimedia_codecs=args.multimedia_codecs,
            with_flathub=args.with_flathub,
            with_zram=args.with_zram,
            with_offline_repo=args.with_offline_repo,
            offline_repo_packages=parsed_offline_packages,
            force_isolated_toolchain=args.force_isolated_toolchain,
            use_tarball=args.use_tarball,
            create_tarball=args.create_tarball,
            verify=bool(args.verify),
            use_tmpfs=args.tmpfs,
            fast=args.fast,
            benchmark=args.benchmark,
            disk_size=args.disk_size,
            filesystem=args.filesystem,
        )
    except (ConfigLoaderError, BuildOrchestratorError) as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.validate_only:
        print(f"\n🔍 Validating configuration for '{arch_lower}' / '{args.distro}'...")
        report = orchestrator.validate()
        if report.get("valid"):
            print("✅ Configuration is VALID!")
        else:
            print("❌ Configuration ERRORS:", report.get("errors"))
        sys.exit(0 if report.get("valid") else 1)

    print(f"🚀 Starting SUSE-Builder [{args.mode.upper()} MODE] for {arch_lower} ({args.distro})...")
    try:
        artifact = orchestrator.build(output_name=args.output)
    except (BuildOrchestratorError, ToolchainManagerError, ZypperManagerError, ISOEngineError, DiskEngineError, ContainerEngineError, StageManagerError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"🎉 Build completed successfully! Output: {artifact}")


if __name__ == "__main__":
    main()
