from __future__ import annotations

import json
import time
from typing import Any, Callable

RESULT_CHUNK_TYPE = "result_chunk"


class ResultChunkReassembler:
    """Reassembles oversized device command results delivered as UDP chunks.

    Large command responses (e.g. ``status``, ~2.5 KB) exceed a single UDP
    datagram, so the firmware splits them into ``result_chunk`` frames keyed by
    ``request_id``. Each chunk carries a slice of the full result-JSON string in
    its ``data`` field. Once every slice for a request_id has arrived, they are
    concatenated in order and parsed back into the original result payload.
    """

    def __init__(self, *, ttl_sec: float = 10.0, now: Callable[[], float] | None = None) -> None:
        self._ttl = max(1.0, float(ttl_sec))
        self._now = now or time.monotonic
        self._buffers: dict[str, dict[str, Any]] = {}

    def pending_count(self) -> int:
        return len(self._buffers)

    def purge(self) -> None:
        cutoff = self._now() - self._ttl
        for request_id in [rid for rid, buf in self._buffers.items() if buf["ts"] < cutoff]:
            self._buffers.pop(request_id, None)

    def add(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Feed one ``result_chunk`` frame.

        Returns a fully-formed result frame
        (``{"type": "result", "device_uid", "request_id", "payload"}``) once all
        chunks for the request_id have been collected, otherwise ``None``.
        """
        request_id = str(frame.get("request_id") or "")
        if not request_id:
            return None
        try:
            index = int(frame.get("chunk"))
            total = int(frame.get("chunks"))
        except (TypeError, ValueError):
            return None
        if total <= 0 or index < 0 or index >= total:
            return None
        data = frame.get("data")
        if not isinstance(data, str):
            return None
        device_uid = str(frame.get("device_uid") or "")

        self.purge()
        buf = self._buffers.get(request_id)
        if buf is None or buf["total"] != total:
            buf = {"parts": {}, "total": total, "device_uid": device_uid, "ts": self._now()}
            self._buffers[request_id] = buf
        buf["parts"][index] = data
        buf["ts"] = self._now()
        if device_uid:
            buf["device_uid"] = device_uid

        if len(buf["parts"]) < buf["total"]:
            return None

        self._buffers.pop(request_id, None)
        try:
            assembled = "".join(buf["parts"][i] for i in range(buf["total"]))
            payload = json.loads(assembled)
        except (KeyError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "type": "result",
            "device_uid": buf["device_uid"],
            "request_id": request_id,
            "payload": payload,
        }
