from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/wenzi7777/New-Horizons-Gateway/main/releases/gateway-latest.json"
ALLOWED_UPDATE_ENTRIES = (
    "newhorizons_gateway",
    "scripts",
    "requirements.txt",
    "README.md",
)


class GatewayUpdateManager:
    def __init__(
        self,
        app_root: str | Path | None = None,
        staging_root: str | Path | None = None,
        manifest_url: str | None = None,
    ) -> None:
        self.app_root = Path(app_root or os.getenv("NEWHORIZONS_GATEWAY_APP_ROOT") or Path(__file__).resolve().parents[1])
        self.staging_root = Path(staging_root or os.getenv("NEWHORIZONS_GATEWAY_UPDATE_DIR") or self.app_root / ".run" / "updates")
        self.manifest_url = manifest_url or os.getenv("NEWHORIZONS_GATEWAY_UPDATE_MANIFEST") or DEFAULT_MANIFEST_URL
        self.latest_manifest: dict[str, Any] = {}
        self.downloaded_zip = self.staging_root / "gateway-update.zip"
        self.downloaded_sha256 = ""
        self.phase = "idle"
        self.last_error = ""
        self.restart_required = False
        self.manual_update_required = False
        self.checked_at = ""

    def state(self) -> dict[str, Any]:
        latest_version = str(self.latest_manifest.get("version") or "")
        return {
            "phase": self.phase,
            "current_version": __version__,
            "latest_version": latest_version,
            "update_available": bool(latest_version and latest_version != __version__),
            "manifest_url": self.manifest_url,
            "zip_url": str(self.latest_manifest.get("zip_url") or ""),
            "notes_url": str(self.latest_manifest.get("notes_url") or ""),
            "sha256": str(self.latest_manifest.get("sha256") or ""),
            "downloaded": self.downloaded_zip.exists(),
            "downloaded_sha256": self.downloaded_sha256,
            "restart_required": self.restart_required,
            "manual_update_required": self.manual_update_required,
            "self_update_supported": self.self_update_supported(),
            "checked_at": self.checked_at,
            "last_error": self.last_error,
        }

    def check(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self.manifest_url, timeout=12) as response:
                payload = response.read()
            manifest = json.loads(payload.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("invalid_manifest")
            for key in ("version", "zip_url", "sha256"):
                if not str(manifest.get(key) or "").strip():
                    raise ValueError(f"manifest_missing_{key}")
            self.latest_manifest = manifest
            self.phase = "checked"
            self.last_error = ""
            self.checked_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            self.phase = "error"
            self.last_error = str(exc)
        return self.state()

    def download(self) -> dict[str, Any]:
        if not self.latest_manifest:
            self.check()
        zip_url = str(self.latest_manifest.get("zip_url") or "")
        expected_sha = str(self.latest_manifest.get("sha256") or "").lower()
        if not zip_url or not expected_sha:
            self.phase = "error"
            self.last_error = "manifest_not_ready"
            return self.state()
        try:
            self.staging_root.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(zip_url, timeout=30) as response:
                payload = response.read()
            actual_sha = hashlib.sha256(payload).hexdigest()
            if actual_sha.lower() != expected_sha:
                raise ValueError("sha256_mismatch")
            self.downloaded_zip.write_bytes(payload)
            self.downloaded_sha256 = actual_sha
            self.phase = "downloaded"
            self.last_error = ""
        except Exception as exc:
            self.phase = "error"
            self.last_error = str(exc)
        return self.state()

    def apply(self) -> dict[str, Any]:
        if not self.downloaded_zip.exists():
            self.download()
        if not self.downloaded_zip.exists():
            return self.state()
        if not self.self_update_supported():
            self.manual_update_required = True
            self.phase = "manual_update_required"
            self.last_error = ""
            return self.state()
        try:
            extract_dir = Path(tempfile.mkdtemp(prefix="gateway-update-", dir=str(self.staging_root)))
            with zipfile.ZipFile(self.downloaded_zip) as archive:
                archive.extractall(extract_dir)
            source_root = self._extracted_source_root(extract_dir)
            backup = self.staging_root / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            backup.mkdir(parents=True, exist_ok=True)
            for name in ALLOWED_UPDATE_ENTRIES:
                src = source_root / name
                if not src.exists():
                    continue
                dst = self.app_root / name
                if dst.exists():
                    shutil.move(str(dst), str(backup / name))
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            self.restart_required = True
            self.phase = "applied"
            self.last_error = ""
        except Exception as exc:
            self.phase = "error"
            self.last_error = str(exc)
        return self.state()

    def restart(self) -> dict[str, Any]:
        command = os.getenv("NEWHORIZONS_GATEWAY_RESTART_COMMAND", "").strip()
        if not command:
            self.manual_update_required = True
            self.phase = "restart_required"
            return self.state()
        try:
            subprocess.Popen(command, cwd=str(self.app_root), shell=True)
            self.phase = "restarting"
            self.last_error = ""
        except Exception as exc:
            self.phase = "error"
            self.last_error = str(exc)
        return self.state()

    @staticmethod
    def _extracted_source_root(extract_dir: Path) -> Path:
        entries = [path for path in extract_dir.iterdir()]
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extract_dir

    @staticmethod
    def self_update_supported() -> bool:
        return os.getenv("NEWHORIZONS_GATEWAY_ALLOW_SELF_UPDATE", "0") == "1"
