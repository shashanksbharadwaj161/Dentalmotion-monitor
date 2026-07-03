from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable

FINDME_DISCOVER_TYPE = "findme_discover"
FINDME_OFFER_TYPE = "findme_offer"
FINDME_PROBE_TYPE = "findme_probe"


class DiscoveryResponder:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        gateway_id: str,
        udp_port: Callable[[], int] | int,
        priority: int = 100,
        gateway_name: str = "New Horizons Gateway",
        upstream_status: Callable[[], str] | None = None,
        is_denied: Callable[[str], bool] | None = None,
        active_claim: Callable[[str], dict | None] | None = None,
        on_request: Callable[[dict, tuple[str, int], bool, str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.gateway_id = gateway_id
        self._udp_port = udp_port
        self.priority = int(priority)
        self.gateway_name = gateway_name
        self.upstream_status = upstream_status or (lambda: "online")
        self.is_denied = is_denied or (lambda _uid: False)
        self.active_claim = active_claim or (lambda _uid: None)
        self.on_request = on_request
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started = False
        self.bound_port = int(port)
        self.last_error = ""

    def start(self) -> None:
        if self._thread is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.5)
        self.bound_port = int(sock.getsockname()[1])
        self._sock = sock
        self._stop.clear()
        self.started = True
        self._thread = threading.Thread(target=self._run, name="newhorizons-gateway-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        self.started = False

    def send_probe(self) -> None:
        """Broadcast a findme_probe so attached devices announce themselves."""
        sock = self._sock
        if sock is None:
            return
        packet = json.dumps({
            "type": FINDME_PROBE_TYPE,
            "gateway_id": self.gateway_id,
            "gateway_name": self.gateway_name,
            "udp_port": self._value(self._udp_port),
        }, separators=(",", ":")).encode("utf-8")
        try:
            sock.sendto(packet, ("255.255.255.255", self.port))
        except OSError as exc:
            self.last_error = str(exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.last_error = str(exc)
                return
            obj = self._decode_request(data)
            if obj is None:
                continue
            device_uid = str(obj.get("device_uid") or "").strip().upper()

            # Device is already attached to another gateway (probe response).
            # Record as nearby but do NOT send an offer — avoid stealing.
            current_gw = str(obj.get("current_gateway_id") or "")
            if current_gw and current_gw != self.gateway_id:
                if self.on_request is not None:
                    self.on_request(obj, addr, False, "device_attached_elsewhere")
                continue

            preferred_gw = str(obj.get("preferred_gateway_id") or "")
            discover_claim_id = str(obj.get("claim_id") or "")
            if preferred_gw and preferred_gw != self.gateway_id:
                decline_reason = "device_switching_gateway"
                if self.on_request is not None:
                    self.on_request(obj, addr, False, decline_reason)
                try:
                    decline = {
                        "type": FINDME_OFFER_TYPE,
                        "device_uid": device_uid,
                        "gateway_id": self.gateway_id,
                        "accept": False,
                        "reason": decline_reason,
                    }
                    sock.sendto(json.dumps(decline, separators=(",", ":")).encode("utf-8"), addr)
                except OSError as exc:
                    self.last_error = str(exc)
                continue
            denied = bool(device_uid and self.is_denied(device_uid))
            reason = "device_rejected" if denied else ""
            claim = self.active_claim(device_uid) if device_uid and not denied else None
            claim_id = str((claim or {}).get("claim_id") or "")
            # When we are the preferred gateway, mirror the device's own claim_id back
            # even if our internal claim record is gone or was marked failed by the
            # upstream server.  This ensures the firmware's claimId_ == offer.claimId
            # check passes and the transfer completes successfully.
            if not claim_id and preferred_gw == self.gateway_id and discover_claim_id and not denied:
                claim_id = discover_claim_id
            response = self._offer_payload(denied=denied, claim_id=claim_id)
            if claim_id:
                response["claim_id"] = claim_id
            if denied:
                response.update({"reason": reason, "cooldown_ms": 30000})
            if self.on_request is not None:
                self.on_request(obj, addr, not denied, reason)
            try:
                response["type"] = FINDME_OFFER_TYPE
                response["device_uid"] = device_uid
                sock.sendto(json.dumps(response, separators=(",", ":")).encode("utf-8"), addr)
            except OSError as exc:
                self.last_error = str(exc)

    def _decode_request(self, data: bytes) -> dict[str, object] | None:
        try:
            decoded = json.loads(data.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(decoded, dict) or decoded.get("type") != FINDME_DISCOVER_TYPE:
            return None
        payload = dict(decoded)
        payload.pop("type", None)
        if not payload.get("device_uid"):
            return None
        return payload

    def _offer_payload(self, *, denied: bool, claim_id: str = "") -> dict[str, object]:
        return {
            "version": 1,
            "gateway_name": self.gateway_name,
            "gateway_id": self.gateway_id,
            "udp_port": self._value(self._udp_port),
            "priority": self.priority + (10000 if claim_id else 0),
            "accept": not denied,
            "upstream_status": self.upstream_status(),
            "ttl_ms": 10000,
            "server_time": int(time.time() * 1000),
        }

    @staticmethod
    def _value(value: Callable[[], int] | int) -> int:
        if callable(value):
            return int(value())
        return int(value)
