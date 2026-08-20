from __future__ import annotations

from typing import Any, Callable


CommandCallback = Callable[[str, dict[str, Any]], None]
MessageCallback = Callable[[dict[str, Any]], None]


class LocalUpstream:
    """Stand-in for a cloud relay client.

    This gateway is local-only: sensor data and control never leave this
    machine's LAN. Nothing here opens a network connection; it exists so the
    rest of the gateway (which was written to relay through an upstream
    client) doesn't need special-casing for the local-only case.
    """

    def __init__(
        self,
        gateway_id: str,
        on_command: CommandCallback | None = None,
        on_message: MessageCallback | None = None,
    ) -> None:
        self.gateway_id = gateway_id
        self.on_command = on_command
        self.on_message = on_message
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def send_device_message(self, msg_type: str, payload: dict[str, Any]) -> None:
        pass

    def send_gateway_status(self, payload: dict[str, Any]) -> None:
        pass

    def send_claim_request(self, device_uid: str, claim_id: str, ttl_ms: int) -> None:
        pass

    def send_packet(self, payload: bytes) -> None:
        pass

    def is_connected(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        return {
            "server_url": "local",
            "connected": self._running,
            "last_error": "",
            "last_connected_at": "",
            "data_queue_size": 0,
            "data_queue_dropped": 0,
            "data_packets_enqueued": 0,
            "data_packets_sent": 0,
            "udp_in_fps": 0,
            "upstream_sent_fps": 0,
            "control_queue_size": 0,
        }

    def update_server(self, server_url: str, auth_token: str | None = None) -> None:
        pass
