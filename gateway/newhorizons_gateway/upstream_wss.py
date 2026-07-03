from __future__ import annotations

import base64
import json
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

try:  # pragma: no cover - depends on optional runtime package.
    import websocket
except Exception:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

from .queueing import DropOldestQueue
from . import __version__


CommandCallback = Callable[[str, dict[str, Any]], None]
MessageCallback = Callable[[dict[str, Any]], None]
PACKET_TEXT_PREFIX = "NHPKT1:"


class UpstreamWSSClient:
    def __init__(
        self,
        server_url: str,
        gateway_id: str,
        auth_token: str = "",
        on_command: CommandCallback | None = None,
        on_message: MessageCallback | None = None,
        data_queue_size: int = 32,
        control_queue_size: int = 512,
    ) -> None:
        self.server_url = server_url
        self.gateway_id = gateway_id
        self.auth_token = auth_token
        self.on_command = on_command
        self.on_message = on_message
        self.data_queue: DropOldestQueue[bytes] = DropOldestQueue(data_queue_size)
        self.control_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(control_queue_size)))
        self._control_queue_lock = threading.RLock()
        self._running = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._ws: websocket.WebSocket | None = None
        self._ws_lock = threading.RLock()
        self.last_error = ""
        self.last_connected_at = ""
        self._stats_lock = threading.RLock()
        self._data_enqueued = 0
        self._data_sent = 0
        self._data_enqueue_window: deque[float] = deque()
        self._data_sent_window: deque[float] = deque()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="newhorizons-gateway-wss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._connected.clear()
        with self._ws_lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._send_thread is not None:
            self._send_thread.join(timeout=1.0)
            self._send_thread = None

    def send_device_message(self, msg_type: str, payload: dict[str, Any]) -> None:
        upstream_type = {
            "hello": "device_hello",
            "status": "device_status",
            "update_progress": "device_update_progress",
            "result": "device_result",
        }.get(msg_type, "device_status")
        message = {
            "type": upstream_type,
            "gateway_id": self.gateway_id,
            "device_uid": payload.get("device_uid", ""),
            "payload": payload,
        }
        self._put_control(message)

    def send_gateway_status(self, payload: dict[str, Any]) -> None:
        message = {
            "type": "gateway_status",
            "gateway_id": self.gateway_id,
            "payload": payload,
        }
        self._put_control(message, coalesce_type="gateway_status")

    def send_claim_request(self, device_uid: str, claim_id: str, ttl_ms: int) -> None:
        self._put_control(
            {
                "type": "gateway_claim_request",
                "gateway_id": self.gateway_id,
                "device_uid": str(device_uid or "").strip().upper(),
                "claim_id": str(claim_id or ""),
                "ttl_ms": int(ttl_ms or 30000),
            }
        )

    def send_packet(self, payload: bytes) -> None:
        self.data_queue.put(payload)
        self._note_data_enqueued()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def status(self) -> dict[str, Any]:
        with self._stats_lock:
            now = time.monotonic()
            self._purge_rate_window_locked(self._data_enqueue_window, now)
            self._purge_rate_window_locked(self._data_sent_window, now)
            data_enqueued = self._data_enqueued
            data_sent = self._data_sent
            data_in_fps = len(self._data_enqueue_window)
            data_sent_fps = len(self._data_sent_window)
        return {
            "server_url": self.server_url,
            "connected": self.is_connected(),
            "last_error": self.last_error,
            "last_connected_at": self.last_connected_at,
            "data_queue_size": self.data_queue.qsize(),
            "data_queue_dropped": self.data_queue.dropped,
            "data_packets_enqueued": data_enqueued,
            "data_packets_sent": data_sent,
            "udp_in_fps": data_in_fps,
            "upstream_sent_fps": data_sent_fps,
            "control_queue_size": self.control_queue.qsize(),
        }

    def update_server(self, server_url: str, auth_token: str | None = None) -> None:
        self.server_url = server_url
        if auth_token is not None:
            self.auth_token = auth_token
        self._disconnect_current()

    def _put_control(self, message: dict[str, Any], coalesce_type: str = "") -> None:
        message.setdefault("gateway_id", self.gateway_id)
        with self._control_queue_lock:
            if coalesce_type:
                self._drop_queued_control_type_locked(coalesce_type)
            while True:
                try:
                    self.control_queue.put_nowait(message)
                    return
                except queue.Full:
                    # Control is important, but an unbounded queue is worse. Drop the oldest unsent control message
                    # and rely on the next status snapshot to refresh server state.
                    try:
                        self.control_queue.get_nowait()
                    except queue.Empty:
                        return

    def _drop_queued_control_type_locked(self, msg_type: str) -> None:
        retained: list[dict[str, Any]] = []
        while True:
            try:
                item = self.control_queue.get_nowait()
            except queue.Empty:
                break
            if item.get("type") == msg_type:
                continue
            retained.append(item)
        for item in retained:
            try:
                self.control_queue.put_nowait(item)
            except queue.Full:
                break

    def _note_data_enqueued(self) -> None:
        with self._stats_lock:
            now = time.monotonic()
            self._data_enqueued += 1
            self._data_enqueue_window.append(now)
            self._purge_rate_window_locked(self._data_enqueue_window, now)

    def _note_data_sent(self) -> None:
        with self._stats_lock:
            now = time.monotonic()
            self._data_sent += 1
            self._data_sent_window.append(now)
            self._purge_rate_window_locked(self._data_sent_window, now)

    @staticmethod
    def _purge_rate_window_locked(window: deque[float], now: float) -> None:
        cutoff = now - 1.0
        while window and window[0] < cutoff:
            window.popleft()

    def _run(self) -> None:
        backoff = 1.0
        while self._running.is_set():
            try:
                if websocket is None:
                    raise RuntimeError("websocket_client_unavailable")
                headers = []
                if self.auth_token:
                    headers.append("Authorization: Bearer {}".format(self.auth_token))
                ws = websocket.create_connection(self.server_url, header=headers, timeout=10)
                with self._ws_lock:
                    self._ws = ws
                self._connected.set()
                self.last_error = ""
                self.last_connected_at = datetime.now(timezone.utc).isoformat()
                backoff = 1.0
                self._send_json(
                    {
                        "type": "hello",
                        "gateway_id": self.gateway_id,
                        "version": __version__,
                        "connected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._send_thread = threading.Thread(target=self._send_loop, name="newhorizons-gateway-wss-send", daemon=True)
                self._send_thread.start()
                self._receive_loop(ws)
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                self._connected.clear()
                with self._ws_lock:
                    ws = self._ws
                    self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self._running.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    def _receive_loop(self, ws: websocket.WebSocket) -> None:
        while self._running.is_set():
            try:
                raw = ws.recv()
            except Exception as exc:
                if self._is_recv_timeout(exc):
                    continue
                raise
            if raw is None:
                break
            try:
                message = self._decode_json_message(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "command" and self.on_command is not None:
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                device_uid = str(message.get("device_uid") or payload.get("device_uid") or "")
                self.on_command(device_uid, dict(payload))
                continue
            if self.on_message is not None:
                self.on_message(dict(message))

    def _send_loop(self) -> None:
        while self._running.is_set() and self._connected.is_set():
            with self._control_queue_lock:
                try:
                    control = self.control_queue.get_nowait()
                except queue.Empty:
                    control = None
            if control is not None:
                try:
                    self._send_json(control)
                except Exception as exc:
                    self.last_error = str(exc)
                    self._put_control(control)
                    self._disconnect_current()
                    break
                continue
            try:
                packet = self.data_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._send_binary(packet)
                self._note_data_sent()
            except Exception as exc:
                self.last_error = str(exc)
                # Sensor data is lossy by design. Drop this packet and let the main
                # upstream loop reconnect instead of killing the thread with a traceback.
                self._disconnect_current()
                break

    def _disconnect_current(self) -> None:
        self._connected.clear()
        with self._ws_lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _send_json(self, payload: dict[str, Any]) -> None:
        with self._ws_lock:
            if self._ws is None:
                raise RuntimeError("upstream_not_connected")
            self._ws.send(json.dumps(payload, separators=(",", ":")))

    def _send_binary(self, payload: bytes) -> None:
        with self._ws_lock:
            if self._ws is None:
                raise RuntimeError("upstream_not_connected")
            self._ws.send(PACKET_TEXT_PREFIX + base64.b64encode(bytes(payload)).decode("ascii"))

    @staticmethod
    def _decode_json_message(raw: Any) -> dict[str, Any]:
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8")
        else:
            text = str(raw)
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise ValueError("invalid_message")
        return decoded

    @staticmethod
    def _is_recv_timeout(exc: Exception) -> bool:
        name = exc.__class__.__name__
        if name in ("TimeoutError", "WebSocketTimeoutException"):
            return True
        return "timed out" in str(exc).lower()
