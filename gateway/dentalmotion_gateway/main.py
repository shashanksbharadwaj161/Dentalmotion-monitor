from __future__ import annotations

import argparse
import json
import signal
import socket
import time
from typing import Any

from .arduino_protocol import is_arduino_heartbeat_packet, is_arduino_stream_packet
from .config_store import GatewayConfigStore
from .discovery import DiscoveryResponder
from .local_device import LocalUDPIngestServer, packet_device_uid as stream_packet_device_uid
from .result_chunks import RESULT_CHUNK_TYPE, ResultChunkReassembler
from .state import GatewayState
from .udp_control import UDPCommandDispatcher, normalize_device_uid
from .update_manager import GatewayUpdateManager
from .upstream_wss import LocalUpstream
from .web import GatewayWebServer


def main() -> None:
    parser = argparse.ArgumentParser(description="DentalMotion local UDP/JSON to WSS gateway")
    parser.add_argument("--config", help="Path to host gateway config", default=None)
    args = parser.parse_args()
    config_store = GatewayConfigStore(args.config)
    config = config_store.snapshot()
    state = GatewayState()
    _mirror_port = int(config.get("local_mirror_udp_port", 0))
    _mirror_sock: socket.socket | None = None
    if _mirror_port:
        _mirror_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print("DentalMotion Gateway: mirroring UDP packets to 127.0.0.1:{}".format(_mirror_port))
    arduino_hosts: dict[str, str] = {}
    result_chunks = ResultChunkReassembler()
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    def command_expired(payload: dict[str, Any]) -> bool:
        try:
            expires_at_ms = int(payload.get("expires_at_ms") or 0)
        except Exception:
            expires_at_ms = 0
        return bool(expires_at_ms and int(time.time() * 1000) > expires_at_ms)


    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def on_command(device_uid: str, payload: dict[str, Any]) -> None:
        normalized_uid = normalize_device_uid(device_uid)
        if command_expired(payload):
            upstream.send_device_message(
                "result",
                {
                    "device_uid": device_uid,
                    "request_id": payload.get("request_id", ""),
                    "command": payload.get("command", ""),
                    "status": "error",
                    "message": "command_expired",
                },
            )
            return
        if udp_commands.send_command(normalized_uid, payload):
            return
        upstream.send_device_message(
            "result",
            {
                "device_uid": normalized_uid,
                "request_id": payload.get("request_id", ""),
                "command": payload.get("command", ""),
                "status": "error",
                "message": "device_not_connected_to_gateway",
            },
        )

    def on_udp_delivery_timeout(device_uid: str, payload: dict[str, Any]) -> None:
        if str(payload.get("command") or "") == "findme_switch_gateway":
            claim_id = str(payload.get("claim_id") or "")
            if claim_id:
                state.fail_claim_delivery(
                    claim_id,
                    "switch_command_delivery_timeout",
                )
        upstream.send_device_message(
            "result",
            {
                "device_uid": device_uid,
                "request_id": payload.get("request_id", ""),
                "command": payload.get("command", ""),
                "status": "error",
                "message": "command_delivery_timeout",
            },
        )

    def on_upstream_message(message: dict[str, Any]) -> None:
        if message.get("type") == "gateway_claim_update":
            claim_id = str(message.get("claim_id") or "")
            if claim_id:
                state.update_claim(
                    claim_id,
                    state=str(message.get("state") or "updated"),
                    last_error=str(message.get("error") or message.get("message") or ""),
                )

    def send_udp_datagram(payload: bytes, addr: tuple[str, int]) -> None:
        udp_server.send_datagram(payload, addr)

    upstream = LocalUpstream(
        gateway_id=str(config["gateway_id"]),
        on_command=on_command,
        on_message=on_upstream_message,
    )
    udp_commands = UDPCommandDispatcher(send_udp_datagram, on_delivery_timeout=on_udp_delivery_timeout)

    def handle_udp_control(payload: bytes, addr: tuple[str, int]) -> None:
        try:
            frame = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(frame, dict):
            return
        if str(frame.get("type") or "") == RESULT_CHUNK_TYPE:
            # Oversized results are split into UDP chunks; reassemble before
            # processing. Returns None until every chunk for the request arrives.
            frame = result_chunks.add(frame)
            if frame is None:
                return
        frame_payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        device_uid = normalize_device_uid(frame.get("device_uid")) or normalize_device_uid(frame_payload.get("device_uid")) or normalize_device_uid(frame_payload.get("device_id"))
        if not device_uid or config_store.is_denied(device_uid):
            return
        udp_commands.set_session(device_uid, addr)
        message_type = str(frame.get("type") or "")
        data = dict(frame_payload)
        data.setdefault("device_uid", device_uid)
        data.setdefault("request_id", frame.get("request_id", ""))
        data.setdefault("received_at_ms", int(time.time() * 1000))
        if message_type == "ack":
            udp_commands.handle_ack(device_uid, frame)
            return
        if message_type in ("hello", "status", "update_progress", "result"):
            if message_type == "result":
                if "cmd" in data and "command" not in data:
                    data["command"] = data["cmd"]
                if "ok" in data and "status" not in data:
                    data["status"] = "ok" if data.get("ok") else "error"
                data.setdefault("transport_path", "arduino_udp")
                udp_commands.handle_result(device_uid, data)
            state.record_control(device_uid, message_type, data, addr, connected=True)
            upstream.send_device_message(message_type, data)

    def on_udp_packet(payload: bytes, addr: tuple[str, int]) -> None:
        if _mirror_sock is not None:
            try:
                _mirror_sock.sendto(payload, ("127.0.0.1", _mirror_port))
            except OSError:
                pass
        if payload.lstrip().startswith(b"{"):
            handle_udp_control(payload, addr)
            return
        is_arduino = is_arduino_stream_packet(payload)
        is_heartbeat = is_arduino_heartbeat_packet(payload)
        device_uid = stream_packet_device_uid(payload)
        denied = bool(device_uid and config_store.is_denied(device_uid))
        same_arduino_peer = bool(arduino_hosts.get(device_uid or "") == addr[0])
        if is_heartbeat and device_uid and not denied:
            arduino_hosts[device_uid] = addr[0]
            udp_commands.set_session(device_uid, addr)
            state.record_heartbeat(
                device_uid,
                {
                    "device_uid": device_uid,
                    "device_name": "DentalMotion Monitor-{}".format(device_uid),
                    "protocol": "DentalMotion/1",
                    "transport_path": "arduino_heartbeat",
                },
                addr,
            )
        elif is_arduino and device_uid and not denied and (state.findme_allows_stream(device_uid, addr) or same_arduino_peer):
            arduino_hosts[device_uid] = addr[0]
            udp_commands.set_session(device_uid, addr)
            state.record_control(
                device_uid,
                "status",
                {
                    "device_uid": device_uid,
                    "device_name": "DentalMotion Monitor-{}".format(device_uid),
                    "protocol": "DentalMotion/1",
                    "transport_path": "arduino_udp",
                },
                addr,
                connected=True,
            )
        serving = bool(device_uid and (is_arduino or state.is_serving(device_uid)))
        forwarded = bool(device_uid and not denied and serving and not is_heartbeat)
        if device_uid and not is_heartbeat:
            state.record_udp_packet(device_uid, len(payload), forwarded)
        if forwarded:
            upstream.send_packet(payload)

    udp_server = LocalUDPIngestServer(str(config["listen_udp_host"]), int(config["listen_udp_port"]), on_udp_packet)
    discovery = DiscoveryResponder(
        str(config["listen_discovery_host"]),
        int(config["listen_discovery_port"]),
        gateway_id=str(config["gateway_id"]),
        udp_port=lambda: int(udp_server.bound_port),
        priority=int(config["discovery_priority"]),
        gateway_name=str(config["gateway_name"]),
        upstream_status=lambda: "online" if upstream.is_connected() else "offline",
        is_denied=config_store.is_denied,
        active_claim=state.active_claim_for,
        on_request=state.record_findme_request,
    ) if config.get("discovery_enabled") else None

    runtime_started = False

    def apply_runtime_config(current_config: dict[str, Any]) -> None:
        nonlocal runtime_started
        upstream.gateway_id = str(current_config.get("gateway_id") or "")
        if discovery is not None:
            discovery.gateway_id = str(current_config.get("gateway_id") or "")
            discovery.gateway_name = str(current_config.get("gateway_name") or "DentalMotion Gateway")
            discovery.priority = int(current_config.get("discovery_priority", 100))
        should_run = bool(current_config.get("enabled"))
        if should_run and not runtime_started:
            upstream.start()
            udp_server.start()
            if discovery is not None:
                discovery.start()
            runtime_started = True
        elif not should_run and runtime_started:
            if discovery is not None:
                discovery.stop()
            udp_server.stop()
            upstream.stop()
            runtime_started = False

    update_manager = GatewayUpdateManager()
    web_server = GatewayWebServer(
        str(config["listen_web_host"]),
        int(config["listen_web_port"]),
        config_store,
        state,
        upstream,
        udp_commands,
        discovery=discovery,
        on_config_saved=apply_runtime_config,
        update_manager=update_manager,
    )

    web_server.start()
    apply_runtime_config(config)
    print(
        "DentalMotion Gateway web=http://127.0.0.1:{} enabled={} udp={}:{} findme={}:{}".format(
            config["listen_web_port"],
            bool(config.get("enabled")),
            config["listen_udp_host"],
            config["listen_udp_port"],
            config["listen_discovery_host"],
            config["listen_discovery_port"],
        )
    )
    last_gateway_status = 0.0
    try:
        while running:  # noqa: SIM117
            time.sleep(0.5)
            now = time.time()
            if now - last_gateway_status >= 5.0:
                last_gateway_status = now
                current_config = config_store.snapshot()
                upstream.send_gateway_status({
                    "gateway_name": current_config.get("gateway_name", "DentalMotion Gateway"),
                    "gateway_id": current_config.get("gateway_id", ""),
                    "enabled": bool(current_config.get("enabled")),
                    "version": update_manager.state().get("current_version", ""),
                    "listen_udp_port": int(udp_server.bound_port),
                    "listen_discovery_port": int(discovery.bound_port) if discovery is not None else int(current_config.get("listen_discovery_port", 22346)),
                    "upstream": upstream.status(),
                    "udp_control": udp_commands.snapshot(),
                    "state": state.snapshot(current_config.get("denied_devices", [])),
                })
            udp_commands.service()
            result_chunks.purge()
    finally:
        if _mirror_sock is not None:
            _mirror_sock.close()
        if discovery is not None:
            discovery.stop()
        web_server.stop()
        udp_server.stop()
        upstream.stop()


if __name__ == "__main__":
    main()
