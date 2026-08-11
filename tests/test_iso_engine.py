import pytest
from pathlib import Path
import subprocess
from suse_builder.core.toolchain_manager import ToolchainManager
from suse_builder.core.iso_engine import ISOEngine

class TestISOEngine:
    def test_mock_iso_build(self, tmp_path):
        workdir = tmp_path / "x86_64"
        target_root = workdir / "chroot"
        toolchain = ToolchainManager(workdir, mode="mock")
        engine = ISOEngine(workdir, target_root, "test-suse", {"architecture": "x86_64"}, mode="mock", toolchain=toolchain)
        iso_path = engine.build_iso()
        assert isinstance(iso_path, Path)
        assert iso_path.name.endswith(".iso")

    def test_grub_standalone_uses_positional_output_arg(self, tmp_path):
        workdir = tmp_path / "x86_64"
        target_root = workdir / "chroot"
        iso_staging = workdir / "iso_root"
        (iso_staging / "boot" / "grub2").mkdir(parents=True, exist_ok=True)
        (iso_staging / "boot" / "grub2" / "grub.cfg").write_text("set timeout=1\n")

        class FakeToolchain:
            def __init__(self):
                self.use_isolated = True
                self.calls = []
                self.embedded_cfg = ""

            def run_tool(self, tool_binary, args, check=True):
                self.calls.append((tool_binary, args))
                if tool_binary in {"grub2-mkstandalone", "grub-mkstandalone"}:
                    out_idx = args.index("-o")
                    embed_arg = next((arg for arg in args if str(arg).startswith("boot/grub/grub.cfg=")), "")
                    if embed_arg:
                        _, cfg_path = embed_arg.split("=", 1)
                        self.embedded_cfg = Path(cfg_path).read_text()
                    Path(args[out_idx + 1]).parent.mkdir(parents=True, exist_ok=True)
                    Path(args[out_idx + 1]).write_bytes(b"EFI")
                elif tool_binary == "truncate":
                    Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                    Path(args[-1]).touch()
                return subprocess.CompletedProcess(args=[tool_binary, *args], returncode=0)

        toolchain = FakeToolchain()
        engine = ISOEngine(
            workdir,
            target_root,
            "test-suse",
            {"architecture": "x86_64"},
            mode="real",
            toolchain=toolchain,
        )

        engine.generate_grub_efi_image()

        grub_calls = [args for binary, args in toolchain.calls if binary in {"grub2-mkstandalone", "grub-mkstandalone"}]
        assert grub_calls
        assert all("-o" in args for args in grub_calls)
        assert all(not any(arg.startswith("-o=") for arg in args) for args in grub_calls)
        assert all(any(str(arg).startswith("boot/grub/grub.cfg=") for arg in args) for args in grub_calls)
        assert "search --no-floppy --set=root --file /boot/mbrid" in toolchain.embedded_cfg
        assert "set prefix=($root)/boot/grub2" in toolchain.embedded_cfg
        assert "configfile ($root)/boot/grub2/grub.cfg" in toolchain.embedded_cfg
