from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


GATEWAY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "gateway_name": "DentalMotion Gateway",
    "gateway_id": "",
    "listen_udp_host": "0.0.0.0",
    "listen_udp_port": 13250,
    "listen_discovery_host": "0.0.0.0",
    "listen_discovery_port": 22346,
    "listen_web_host": "0.0.0.0",
    "listen_web_port": 5052,
    "discovery_enabled": True,
    "discovery_priority": 100,
    "denied_devices": [],
}


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def validate_gateway_id(value: Any) -> str:
    gateway_id = str(value or "").strip()
    if not gateway_id:
        raise ValueError("gateway_id_required")
    if not GATEWAY_ID_PATTERN.fullmatch(gateway_id):
        raise ValueError("gateway_id_invalid")
    return gateway_id


class GatewayConfigStore:
    """Host-only admin config storage.

    Device control now uses JSON control messages. Sensor telemetry remains the
    compact UDP packet stream.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or os.getenv("DENTALMOTION_GATEWAY_CONFIG", "gateway_config.json"))
        self.config = self._load()

    def _load(self) -> dict[str, Any]:
        config = dict(DEFAULT_CONFIG)
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        self._apply_env(config)
        self._normalize(config)
        return config

    def _apply_env(self, config: dict[str, Any]) -> None:
        env_map = {
            "DENTALMOTION_GATEWAY_UDP_HOST": "listen_udp_host",
            "DENTALMOTION_GATEWAY_UDP_PORT": "listen_udp_port",
            "DENTALMOTION_GATEWAY_DISCOVERY_HOST": "listen_discovery_host",
            "DENTALMOTION_GATEWAY_DISCOVERY_PORT": "listen_discovery_port",
            "DENTALMOTION_GATEWAY_WEB_HOST": "listen_web_host",
            "DENTALMOTION_GATEWAY_WEB_PORT": "listen_web_port",
            "DENTALMOTION_GATEWAY_DISCOVERY_ENABLED": "discovery_enabled",
            "DENTALMOTION_GATEWAY_DISCOVERY_PRIORITY": "discovery_priority",
            "DENTALMOTION_GATEWAY_ID": "gateway_id",
            "DENTALMOTION_GATEWAY_ENABLED": "enabled",
            "DENTALMOTION_GATEWAY_NAME": "gateway_name",
        }
        for env_name, key in env_map.items():
            value = os.getenv(env_name)
            if value not in (None, ""):
                config[key] = value

    def _normalize(self, config: dict[str, Any]) -> None:
        for key in ("listen_udp_port", "listen_discovery_port", "listen_web_port", "discovery_priority"):
            config[key] = int(config[key])
        config["enabled"] = parse_bool(config.get("enabled"), default=False)
        config["discovery_enabled"] = parse_bool(config.get("discovery_enabled"), default=True)
        config["gateway_id"] = str(config.get("gateway_id") or "").strip()
        if config["gateway_id"] and not GATEWAY_ID_PATTERN.fullmatch(config["gateway_id"]):
            config["gateway_id"] = ""
            config["enabled"] = False
        if not isinstance(config.get("denied_devices"), list):
            config["denied_devices"] = []
        config["denied_devices"] = sorted({str(uid).strip().upper() for uid in config["denied_devices"] if str(uid).strip()})

    def snapshot(self) -> dict[str, Any]:
        return dict(self.config)

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        next_config = dict(self.config)
        next_config.update(patch)
        self._normalize(next_config)
        if next_config.get("gateway_id"):
            next_config["gateway_id"] = validate_gateway_id(next_config["gateway_id"])
        if next_config.get("enabled"):
            next_config["gateway_id"] = validate_gateway_id(next_config.get("gateway_id"))
        self.config = next_config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, self.path)
        return self.snapshot()

    def is_denied(self, device_uid: str) -> bool:
        return str(device_uid).strip().upper() in set(self.config.get("denied_devices", []))

    def deny(self, device_uid: str) -> dict[str, Any]:
        uid = str(device_uid).strip().upper()
        denied = set(self.config.get("denied_devices", []))
        if uid:
            denied.add(uid)
        return self.save({"denied_devices": sorted(denied)})

    def allow(self, device_uid: str) -> dict[str, Any]:
        uid = str(device_uid).strip().upper()
        denied = {item for item in self.config.get("denied_devices", []) if item != uid}
        return self.save({"denied_devices": sorted(denied)})
