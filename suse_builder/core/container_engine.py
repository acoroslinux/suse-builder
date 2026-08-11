"""Create portable OCI image-layout archives from a prepared root filesystem."""

import hashlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from suse_builder.core.path_utils import resolve_from_project


class ContainerEngineError(Exception):
    """Raised when an OCI archive cannot be created."""


class ContainerEngine:
    def __init__(self, target_root: Path, output_name: str, config: Dict[str, Any], mode: str):
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()

    @staticmethod
    def _descriptor(data: bytes, media_type: str) -> Dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        return {"mediaType": media_type, "digest": f"sha256:{digest}", "size": len(data)}, digest

    def build_oci_archive(self) -> Path:
        output_path = resolve_from_project(f"output/{self.output_name}.oci.tar")
        layout_dir = self.target_root.parent / "oci-layout"
        if layout_dir.exists():
            shutil.rmtree(layout_dir)
        blobs_dir = layout_dir / "blobs" / "sha256"
        blobs_dir.mkdir(parents=True)
        (layout_dir / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')

        layer_path = layout_dir / "layer.tar"
        with tarfile.open(layer_path, "w") as layer:
            if self.target_root.exists():
                for child in sorted(self.target_root.iterdir()):
                    layer.add(child, arcname=child.name, recursive=True)
        layer_data = layer_path.read_bytes()
        layer_desc, layer_digest = self._descriptor(layer_data, "application/vnd.oci.image.layer.v1.tar")
        (blobs_dir / layer_digest).write_bytes(layer_data)
        layer_path.unlink()

        created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        config_data = json.dumps({"created": created, "architecture": self.config.get("arch", "x86_64"), "os": "linux", "config": {"Cmd": ["/bin/sh"]}, "rootfs": {"type": "layers", "diff_ids": [layer_desc["digest"]]}}, separators=(",", ":")).encode()
        config_desc, config_digest = self._descriptor(config_data, "application/vnd.oci.image.config.v1+json")
        (blobs_dir / config_digest).write_bytes(config_data)
        manifest_data = json.dumps({"schemaVersion": 2, "config": config_desc, "layers": [layer_desc]}, separators=(",", ":")).encode()
        manifest_desc, manifest_digest = self._descriptor(manifest_data, "application/vnd.oci.image.manifest.v1+json")
        (blobs_dir / manifest_digest).write_bytes(manifest_data)
        index = {"schemaVersion": 2, "manifests": [{**manifest_desc, "annotations": {"org.opencontainers.image.ref.name": self.output_name}}]}
        (layout_dir / "index.json").write_text(json.dumps(index, separators=(",", ":")))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w") as archive:
            archive.add(layout_dir / "oci-layout", arcname="oci-layout")
            archive.add(layout_dir / "index.json", arcname="index.json")
            archive.add(layout_dir / "blobs", arcname="blobs")
        shutil.rmtree(layout_dir)
        return output_path
