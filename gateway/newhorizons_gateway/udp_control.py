from __future__ import annotations

import json
import time
import threading
from typing import Any, Callable

Address = tuple[str, int]
SendDatagram = Callable[[bytes, Address], None]
DeliveryTimeout = Callable[[str, dict[str, Any]], None]


def normalize_device_uid(value: Any) -> str:
    uid = str(value or "").replace(":", "").replace("-", "").replace(" ", "").strip().upper()
    if not uid or uid == "000000000000":
        return ""
    return uid


class UDPCommandDispatcher:
    RESEND_INTERVAL_SEC = 0.5
    MAX_UNACKED_ATTEMPTS = 30

    def __init__(
        self,
        send_datagram: SendDatagram,
        *,
        on_delivery_timeout: DeliveryTimeout | None = None,
        now: Callable[[], float] | None = None,
        session_ttl_sec: float = 90.0,
    ) -> None:
        self.send_datagram = send_datagram
        self.on_delivery_timeout = on_delivery_timeout
        self.now = now or time.time
        self.session_ttl_sec = max(1.0, float(session_ttl_sec or 90.0))
        self.lock = threading.RLock()
        self.sessions: dict[str, Address] = {}
        self.session_seen_at: dict[str, float] = {}
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.seq = 0
        self.commands_sent = 0
        self.commands_retried = 0
        self.commands_acked = 0
        self.commands_timeout = 0
        self.last_command = ""
        self.last_error = ""

    def set_session(self, device_uid: Any, addr: Address) -> str:
        uid = normalize_device_uid(device_uid)
        if uid:
            with self.lock:
                self.sessions[uid] = addr
                self.session_seen_at[uid] = self.now()
        return uid

    def drop_session(self, device_uid: Any, addr: Address | None = None) -> None:
        uid = normalize_device_uid(device_uid)
        if not uid:
            return
        with self.lock:
            if addr is None or self.sessions.get(uid) == addr:
                self.sessions.pop(uid, None)
                self.session_seen_at.pop(uid, None)

    def send_command(self, device_uid: Any, payload: dict[str, Any]) -> bool:
        uid = normalize_device_uid(device_uid)
        if not uid:
            return False
        with self.lock:
            self._prune_stale_sessions_locked(self.now())
            addr = self.sessions.get(uid)
            if addr is None:
                return False
            self._queue_command_locked(uid, addr, payload)
        return True

    def send_command_to(self, device_uid: Any, addr: Address, payload: dict[str, Any]) -> bool:
        uid = normalize_device_uid(device_uid)
        host = str(addr[0] if addr else "").strip()
        try:
            port = int(addr[1])
        except (IndexError, TypeError, ValueError):
            port = 0
        if not uid or not host or port <= 0:
            return False
        with self.lock:
            self._queue_command_locked(uid, (host, port), payload)
        return True

    def handle_ack(self, device_uid: Any, frame: dict[str, Any]) -> None:
        uid = normalize_device_uid(device_uid or frame.get("device_uid"))
        if not uid:
            return
        frame_payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        request_id = str(frame.get("request_id") or frame_payload.get("request_id") or "")
        ack = int(frame.get("ack") or 0)
        with self.lock:
            for key, entry in list(self.pending.items()):
                if entry.get("device_uid") != uid:
                    continue
                if request_id and entry.get("request_id") == request_id:
                    self._mark_acked(entry)
                    return
                if ack and int(entry.get("seq") or 0) == ack:
                    self._mark_acked(entry)
                    return

    def handle_result(self, device_uid: Any, payload: dict[str, Any]) -> None:
        uid = normalize_device_uid(device_uid or payload.get("device_uid"))
        request_id = str((payload or {}).get("request_id") or "")
        if not uid or not request_id:
            return
        with self.lock:
            self.pending.pop((uid, request_id), None)

    def service(self) -> None:
        now = self.now()
        timed_out: list[dict[str, Any]] = []
        with self.lock:
            stale_uids = self._prune_stale_sessions_locked(now)
            for key, entry in list(self.pending.items()):
                expired = self._expired(entry, now)
                if entry.get("acked"):
                    if expired:
                        self.pending.pop(key, None)
                    continue
                if str(entry.get("device_uid") or "") in stale_uids:
                    self.pending.pop(key, None)
                    self.commands_timeout += 1
                    self.last_error = "command_delivery_timeout"
                    timed_out.append(entry)
                    continue
                if expired or (
                    not self._has_explicit_expiry(entry)
                    and int(entry.get("attempts") or 0) >= self.MAX_UNACKED_ATTEMPTS
                ):
                    self.pending.pop(key, None)
                    self.commands_timeout += 1
                    self.last_error = "command_delivery_timeout"
                    timed_out.append(entry)
                    continue
                if now - float(entry.get("last_sent_at") or 0.0) >= self.RESEND_INTERVAL_SEC:
                    self._send_entry(entry, retry=True)
        if self.on_delivery_timeout is not None:
            for entry in timed_out:
                self.on_delivery_timeout(str(entry.get("device_uid") or ""), dict(entry.get("payload") or {}))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._prune_stale_sessions_locked(self.now())
            return {
                "sessions": len(self.sessions),
                "pending": len(self.pending),
                "commands_sent": self.commands_sent,
                "commands_retried": self.commands_retried,
                "commands_acked": self.commands_acked,
                "commands_timeout": self.commands_timeout,
                "last_command": self.last_command,
                "last_error": self.last_error,
            }

    def _mark_acked(self, entry: dict[str, Any]) -> None:
        if not entry.get("acked"):
            entry["acked"] = True
            self.commands_acked += 1
            self.last_error = ""

    def _queue_command_locked(self, uid: str, addr: Address, payload: dict[str, Any]) -> None:
        payload = dict(payload or {})
        request_id = str(payload.get("request_id") or "")
        self.seq = (int(self.seq) + 1) & 0xFFFF
        if self.seq == 0:
            self.seq = 1
        packet = json.dumps(
            {
                "type": "command",
                "device_uid": uid,
                "seq": self.seq,
                "request_id": request_id,
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        key = (uid, request_id or str(self.seq))
        entry = {
            "device_uid": uid,
            "addr": addr,
            "payload": payload,
            "request_id": request_id,
            "seq": self.seq,
            "packet": packet,
            "attempts": 0,
            "acked": False,
            "created_at": self.now(),
            "last_sent_at": 0.0,
        }
        self.pending[key] = entry
        self._send_entry(entry, retry=False)

    def _send_entry(self, entry: dict[str, Any], *, retry: bool) -> None:
        try:
            self.send_datagram(bytes(entry["packet"]), entry["addr"])
        except Exception as exc:
            self.drop_session(entry.get("device_uid"), entry.get("addr"))
            self.last_error = str(exc) or exc.__class__.__name__
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["last_sent_at"] = self.now()
            return
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_sent_at"] = self.now()
        if retry:
            self.commands_retried += 1
        else:
            self.commands_sent += 1
        command = str((entry.get("payload") or {}).get("command") or "")
        request_id = str(entry.get("request_id") or "")
        self.last_command = "{}:{}".format(command, request_id) if request_id else command
        self.last_error = ""

    def _prune_stale_sessions_locked(self, now: float) -> set[str]:
        stale: set[str] = set()
        for uid, seen_at in list(self.session_seen_at.items()):
            if now - float(seen_at or 0.0) <= self.session_ttl_sec:
                continue
            stale.add(uid)
            self.sessions.pop(uid, None)
            self.session_seen_at.pop(uid, None)
        return stale

    @staticmethod
    def _expired(entry: dict[str, Any], now: float) -> bool:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        try:
            expires_at_ms = int(payload.get("expires_at_ms") or 0)
        except Exception:
            expires_at_ms = 0
        return bool(expires_at_ms and int(now * 1000) > expires_at_ms)

    @staticmethod
    def _has_explicit_expiry(entry: dict[str, Any]) -> bool:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        try:
            return int(payload.get("expires_at_ms") or 0) > 0
        except Exception:
            return False
