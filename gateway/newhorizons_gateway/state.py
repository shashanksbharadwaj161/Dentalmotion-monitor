from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewayState:
    def __init__(self, *, now: Callable[[], float] | None = None, control_stale_sec: float = 90.0, findme_attach_window_sec: float = 20.0) -> None:
        self._now = now or time.time
        self.control_stale_sec = max(1.0, float(control_stale_sec or 90.0))
        self.findme_attach_window_sec = max(1.0, float(findme_attach_window_sec or 20.0))
        self._lock = threading.RLock()
        self.devices: dict[str, dict[str, Any]] = {}
        self.findme_requests: deque[dict[str, Any]] = deque(maxlen=120)
        self.claims: dict[str, dict[str, Any]] = {}

    def record_findme_request(self, payload: dict[str, Any], addr: tuple[str, int], accepted: bool, reason: str = "") -> None:
        uid = str(payload.get("device_uid") or "").strip().upper()
        now = utc_now()
        item = {
            "device_uid": uid,
            "device_name": payload.get("device_name", ""),
            "mode": payload.get("mode", ""),
            "firmware_version": payload.get("firmware_version", ""),
            "hardware_model": payload.get("hardware_model", ""),
            "protocol": payload.get("protocol", ""),
            "wifi_rssi": payload.get("wifi_rssi"),
            "addr": addr[0],
            "addr_port": int(addr[1]),
            "accepted": accepted,
            "reason": reason,
            "received_at": now,
            "received_monotonic": self._now(),
        }
        with self._lock:
            self.findme_requests.appendleft(item)
            if uid:
                device = self.devices.setdefault(uid, {"device_uid": uid, "udp_packets": 0, "udp_bytes": 0})
                device.update({
                    "device_name": payload.get("device_name") or device.get("device_name", ""),
                    "mode": payload.get("mode") or device.get("mode", ""),
                    "firmware_version": payload.get("firmware_version") or device.get("firmware_version", ""),
                    "hardware_model": payload.get("hardware_model") or device.get("hardware_model", ""),
                    "protocol": payload.get("protocol") or device.get("protocol", ""),
                    "wifi_rssi": payload.get("wifi_rssi", device.get("wifi_rssi")),
                    "last_findme_at": now,
                    "last_findme_addr": addr[0],
                    "last_findme_monotonic": item["received_monotonic"],
                    "findme_accepted": accepted,
                    "findme_reason": reason,
                    "findme_state": "offered" if accepted else "rejected",
                })

    def record_control(self, device_uid: str, msg_type: str, payload: dict[str, Any], peer: tuple[str, int], connected: bool = True) -> None:
        uid = str(device_uid).strip().upper()
        if not uid:
            return
        now = utc_now()
        with self._lock:
            device = self.devices.setdefault(uid, {"device_uid": uid, "udp_packets": 0, "udp_bytes": 0})
            nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            mode = payload.get("mode") or nested.get("mode") or data.get("mode")
            if not mode and msg_type == "result":
                # Mirror backend's _maintenance_mode_from_result: the firmware does not
                # include a mode field in enter/exit_maintenance acks, so derive the
                # resulting mode from the command/message to keep the gateway's cached
                # state in sync immediately (before the next FindMe broadcast).
                ok = payload.get("ok") is not False and str(payload.get("status") or "") != "error"
                if ok:
                    cmd = str(payload.get("command") or payload.get("cmd") or "")
                    msg = str(payload.get("message") or "")
                    if cmd == "enter_maintenance" or msg == "maintenance_entered":
                        mode = "maintenance"
                    elif cmd == "exit_maintenance" or msg == "maintenance_exited":
                        mode = "normal"
            if not mode:
                mode = device.get("mode", "")
            command = str(payload.get("command") or payload.get("cmd") or "")
            switch_started = msg_type == "result" and command == "findme_switch_gateway"
            firmware_version = (
                payload.get("firmware_version")
                or nested.get("firmware_version")
                or data.get("firmware_version")
                or device.get("firmware_version", "")
            )
            update = {
                "last_seen": now,
                "last_seen_monotonic": self._now(),
                "last_control_type": msg_type,
                "device_name": payload.get("device_name") or device.get("device_name", ""),
                "mode": mode,
                "firmware_version": firmware_version or device.get("firmware_version", ""),
                "protocol": payload.get("protocol") or nested.get("protocol") or data.get("protocol") or device.get("protocol", ""),
                "hardware_model": payload.get("hardware_model") or nested.get("hardware_model") or data.get("hardware_model") or device.get("hardware_model", ""),
                "transport_path": payload.get("transport_path") or nested.get("transport_path") or data.get("transport_path") or device.get("transport_path", ""),
            }
            if not switch_started:
                update.update({
                    "connected": connected,
                    "peer": "{}:{}".format(peer[0], peer[1]),
                    "findme_state": "attached" if connected else "disconnected",
                })
            device.update(update)
            if connected and not switch_started:
                self._attach_active_claim_locked(uid, device, now)

    def record_heartbeat(self, device_uid: str, payload: dict[str, Any], peer: tuple[str, int]) -> None:
        uid = str(device_uid).strip().upper()
        if not uid:
            return
        now = utc_now()
        payload = dict(payload or {})
        with self._lock:
            device = self.devices.setdefault(uid, {"device_uid": uid, "udp_packets": 0, "udp_bytes": 0})
            device.update({
                "connected": True,
                "peer": "{}:{}".format(peer[0], peer[1]),
                "last_seen": now,
                "last_seen_monotonic": self._now(),
                "last_control_type": "heartbeat",
                "last_heartbeat_at": now,
                "findme_state": "attached",
                "device_name": payload.get("device_name") or device.get("device_name", ""),
                # A binary heartbeat is only a liveness signal and does not carry
                # the device's authoritative mode (that comes from FindMe discovery
                # and JSON status).  Never let it overwrite a known mode, otherwise
                # the WebUI flickers between maintenance and normal.
                "mode": device.get("mode") or "normal",
                "protocol": payload.get("protocol") or device.get("protocol", "NHO/Arduino/1"),
                "hardware_model": payload.get("hardware_model") or device.get("hardware_model", ""),
                "firmware_version": payload.get("firmware_version") or device.get("firmware_version", ""),
                "transport_path": payload.get("transport_path") or "arduino_heartbeat",
            })
            self._attach_active_claim_locked(uid, device, now)

    def record_disconnect(self, device_uid: str) -> None:
        uid = str(device_uid).strip().upper()
        if not uid:
            return
        with self._lock:
            device = self.devices.setdefault(uid, {"device_uid": uid, "udp_packets": 0, "udp_bytes": 0})
            device["connected"] = False
            device["disconnected_at"] = utc_now()
            device["findme_state"] = "disconnected"

    def record_udp_packet(self, device_uid: str, byte_count: int, forwarded: bool) -> None:
        uid = str(device_uid).strip().upper()
        if not uid:
            return
        with self._lock:
            device = self.devices.setdefault(uid, {"device_uid": uid, "udp_packets": 0, "udp_bytes": 0})
            device["udp_packets"] = int(device.get("udp_packets", 0)) + 1
            device["udp_bytes"] = int(device.get("udp_bytes", 0)) + int(byte_count)
            if forwarded:
                device["udp_forwarded"] = int(device.get("udp_forwarded", 0)) + 1
            else:
                device["udp_dropped"] = int(device.get("udp_dropped", 0)) + 1
            device["last_udp_at"] = utc_now()

    def is_serving(self, device_uid: str) -> bool:
        uid = str(device_uid).strip().upper()
        with self._lock:
            self._mark_stale_control_locked()
            return bool(self.devices.get(uid, {}).get("connected"))

    def findme_allows_stream(self, device_uid: str, peer: tuple[str, int]) -> bool:
        uid = str(device_uid).strip().upper()
        host = str(peer[0] if peer else "")
        now = self._now()
        with self._lock:
            device = self.devices.get(uid, {})
            if (
                device.get("findme_accepted")
                and device.get("last_findme_addr") == host
                and now - float(device.get("last_findme_monotonic") or 0.0) <= self.findme_attach_window_sec
            ):
                return True
            for request in self.findme_requests:
                if str(request.get("device_uid") or "").strip().upper() != uid:
                    continue
                if not request.get("accepted"):
                    continue
                if str(request.get("addr") or "") != host:
                    continue
                return now - float(request.get("received_monotonic") or 0.0) <= self.findme_attach_window_sec
        return False

    def create_claim(self, device_uid: str, ttl_ms: int = 30000) -> dict[str, Any]:
        uid = str(device_uid).strip().upper()
        ttl_ms = max(1000, int(ttl_ms or 30000))
        now = utc_now()
        expires_at_ms = int(time.time() * 1000) + ttl_ms
        claim = {
            "claim_id": uuid.uuid4().hex,
            "device_uid": uid,
            "state": "created",
            "requested_at": now,
            "updated_at": now,
            "ttl_ms": ttl_ms,
            "expires_at_ms": expires_at_ms,
            "last_error": "",
        }
        with self._lock:
            self.claims[claim["claim_id"]] = claim
        return dict(claim)

    def update_claim(self, claim_id: str, **patch: Any) -> dict[str, Any] | None:
        claim_id = str(claim_id or "")
        with self._lock:
            claim = self.claims.get(claim_id)
            if claim is None:
                return None
            claim.update(patch)
            claim["updated_at"] = utc_now()
            return dict(claim)

    def fail_claim_delivery(self, claim_id: str, error: str) -> dict[str, Any] | None:
        claim_id = str(claim_id or "")
        with self._lock:
            claim = self.claims.get(claim_id)
            if claim is None:
                return None
            if claim.get("state") == "attached":
                return dict(claim)
            claim["state"] = "failed"
            claim["last_error"] = str(error or "switch_command_delivery_timeout")
            claim["updated_at"] = utc_now()
            return dict(claim)

    def active_claim_for(self, device_uid: str) -> dict[str, Any] | None:
        uid = str(device_uid).strip().upper()
        now_ms = int(time.time() * 1000)
        with self._lock:
            claim = self._active_claim_locked(uid, now_ms)
            if claim is not None:
                return dict(claim)
        return None

    def last_findme_addr(self, device_uid: str) -> str | None:
        uid = str(device_uid).strip().upper()
        with self._lock:
            addr = self.devices.get(uid, {}).get("last_findme_addr")
            return str(addr) if addr else None

    def pending_claims(self) -> list[dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        with self._lock:
            result = []
            for claim in self.claims.values():
                if int(claim.get("expires_at_ms") or 0) <= now_ms:
                    continue
                if claim.get("state") in ("attached", "failed", "timeout"):
                    continue
                result.append(dict(claim))
            return result

    def snapshot(self, denied_devices: list[str]) -> dict[str, Any]:
        denied = {str(uid).strip().upper() for uid in denied_devices}
        with self._lock:
            self._mark_stale_control_locked()
            devices = []
            for uid, device in self.devices.items():
                item = dict(device)
                item["denied"] = uid in denied
                devices.append(item)
            devices.sort(key=lambda item: (not bool(item.get("connected")), str(item.get("device_uid", ""))))
            nearby = self._nearby_devices_locked(denied)
            return {
                "devices": devices,
                "denied_devices": sorted(denied),
                "findme_requests": list(self.findme_requests),
                "nearby_devices": nearby,
                "claims": sorted((dict(item) for item in self.claims.values()), key=lambda item: str(item.get("updated_at", "")), reverse=True),
            }

    def _nearby_devices_locked(self, denied: set[str]) -> list[dict[str, Any]]:
        by_uid: dict[str, dict[str, Any]] = {}
        for request in reversed(self.findme_requests):
            uid = str(request.get("device_uid") or "").strip().upper()
            if not uid:
                continue
            by_uid[uid] = {
                "device_uid": uid,
                "device_name": request.get("device_name", ""),
                "mode": request.get("mode", ""),
                "wifi_rssi": request.get("wifi_rssi"),
                "addr": request.get("addr", ""),
                "last_findme_at": request.get("received_at", ""),
                "findme_accepted": bool(request.get("accepted")),
                "findme_reason": request.get("reason", ""),
            }
        for uid, device in self.devices.items():
            item = by_uid.setdefault(uid, {"device_uid": uid})
            item.update({
                "device_name": device.get("device_name") or item.get("device_name", ""),
                "mode": device.get("mode") or item.get("mode", ""),
                "wifi_rssi": device.get("wifi_rssi", item.get("wifi_rssi")),
                "addr": device.get("last_findme_addr") or item.get("addr", ""),
                "last_findme_at": device.get("last_findme_at") or item.get("last_findme_at", ""),
                "serving": bool(device.get("connected")),
                "connected": bool(device.get("connected")),
                "peer": device.get("peer", ""),
                "findme_state": device.get("findme_state") or item.get("findme_state", ""),
                "claim_id": device.get("claim_id", ""),
            })
        for uid, item in by_uid.items():
            item["denied"] = uid in denied
            item.setdefault("serving", False)
            item.setdefault("connected", False)
            claim = self.active_claim_for(uid)
            if claim is not None:
                item["claim_id"] = claim.get("claim_id", "")
                item["claim_state"] = claim.get("state", "")
        return sorted(
            by_uid.values(),
            key=lambda item: (
                not bool(item.get("serving")),
                bool(item.get("denied")),
                str(item.get("device_uid", "")),
            ),
        )

    def _mark_stale_control_locked(self) -> None:
        now = self._now()
        for device in self.devices.values():
            if not device.get("connected"):
                continue
            seen_at = device.get("last_seen_monotonic")
            if seen_at is None:
                continue
            if now - float(seen_at or 0.0) <= self.control_stale_sec:
                continue
            device["connected"] = False
            device["disconnected_at"] = utc_now()
            device["findme_state"] = "disconnected"

    def _active_claim_locked(self, uid: str, now_ms: int | None = None) -> dict[str, Any] | None:
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        for claim in self.claims.values():
            if str(claim.get("device_uid") or "").upper() != uid:
                continue
            if int(claim.get("expires_at_ms") or 0) <= current_ms:
                if claim.get("state") not in ("attached", "timeout", "failed"):
                    claim["state"] = "timeout"
                    claim["last_error"] = "claim_timeout"
                    claim["updated_at"] = utc_now()
                continue
            if claim.get("state") in ("failed", "timeout"):
                continue
            return claim
        return None

    def _attach_active_claim_locked(self, uid: str, device: dict[str, Any], now: str) -> None:
        claim = self._active_claim_locked(uid)
        if claim is None:
            return
        claim["state"] = "attached"
        claim["attached_at"] = now
        claim["updated_at"] = now
        claim["last_error"] = ""
        device["claim_id"] = claim.get("claim_id", "")
