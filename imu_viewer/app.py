"""
New Horizons OS – Local IMU Viewer
Listens for UDP packets mirrored by the gateway on port 13253,
parses 6D IMU data (accel XYZ + gyro XYZ), serves a live Chart.js UI,
and saves sessions to CSV.
"""
from __future__ import annotations

import csv
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

# ── Config ───────────────────────────────────────────────────────────────────
UDP_HOST = "0.0.0.0"
UDP_PORT = int(os.environ.get("NHOS_UDP_PORT", 13253))   # mirror port from gateway
WEB_PORT = int(os.environ.get("NHOS_WEB_PORT", 8765))
DATA_DIR     = Path.home() / "Desktop" / "DentalMotion_Recordings"
GATEWAY_URL  = os.environ.get("NHOS_GATEWAY_URL", "http://127.0.0.1:5052")
DEVICE_UID   = os.environ.get("NHOS_DEVICE_UID",  "3CDC75413DC8")
HISTORY_SECONDS = 15
MAX_HISTORY = 1500    # 100 fps × 15 s

# ── NHOS packet constants ─────────────────────────────────────────────────────
MAGIC = 0xA55A
HEADER_LEN = 20
FLAG_IMU = 0x01
FLAG_HEARTBEAT = 0x80
IMU_FLOATS = 7   # ax ay az gx gy gz 0(reserved)

# ── Shared state ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)
_stats = {
    "packets_received": 0,
    "imu_samples": 0,
    "last_uid": "",
    "last_rx_ms": 0,
    "recording": False,
    "csv_path": "",
    "csv_rows": 0,
}
_csv_file: Any = None
_csv_writer: Any = None

# ── Packet parser ─────────────────────────────────────────────────────────────

def _parse_packet(data: bytes) -> dict[str, Any] | None:
    if len(data) < HEADER_LEN:
        return None
    magic = struct.unpack_from("<H", data, 0)[0]
    if magic != MAGIC:
        return None
    flags = data[3]
    if flags & FLAG_HEARTBEAT:
        return None   # heartbeat, no IMU payload
    if not (flags & FLAG_IMU):
        return None   # no IMU data in this packet
    uid = data[4:10].hex().upper()
    seq = struct.unpack_from("<I", data, 10)[0]
    ts_ms = struct.unpack_from("<I", data, 14)[0]
    payload_len = struct.unpack_from("<H", data, 18)[0]
    imu_bytes = IMU_FLOATS * 4
    matrix_bytes = payload_len - imu_bytes
    if matrix_bytes < 0 or len(data) < HEADER_LEN + matrix_bytes + imu_bytes:
        return None
    imu_off = HEADER_LEN + matrix_bytes
    floats = struct.unpack_from("<7f", data, imu_off)
    return {
        "t": time.time(),
        "ts_ms": ts_ms,
        "seq": seq,
        "uid": uid,
        "ax": round(floats[0], 5),
        "ay": round(floats[1], 5),
        "az": round(floats[2], 5),
        "gx": round(floats[3], 5),
        "gy": round(floats[4], 5),
        "gz": round(floats[5], 5),
    }

# ── UDP listener thread ───────────────────────────────────────────────────────

def _udp_worker() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    try:
        sock.bind((UDP_HOST, UDP_PORT))
        print(f"IMU viewer: UDP listening on {UDP_HOST}:{UDP_PORT}")
    except OSError as e:
        print(f"IMU viewer: FAILED to bind UDP {UDP_PORT}: {e}")
        return

    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break

        sample = _parse_packet(data)
        if sample is None:
            with _lock:
                _stats["packets_received"] += 1
            continue

        with _lock:
            _stats["packets_received"] += 1
            _stats["imu_samples"] += 1
            _stats["last_uid"] = sample["uid"]
            _stats["last_rx_ms"] = int(time.time() * 1000)
            _history.append(sample)
            if _stats["recording"] and _csv_writer is not None:
                _csv_writer.writerow([
                    datetime.fromtimestamp(sample["t"]).isoformat(),
                    sample["seq"],
                    sample["ax"], sample["ay"], sample["az"],
                    sample["gx"], sample["gy"], sample["gz"],
                ])
                _csv_file.flush()
                _stats["csv_rows"] += 1

# ── Gateway helper ────────────────────────────────────────────────────────────

def _gw_request(method: str, path: str, body: dict | None = None) -> dict:
    url  = GATEWAY_URL + path
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    hdrs: dict[str, str] = {"Content-Type": "application/json"} if data else {}
    req  = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode())

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.get("/")
def index() -> str:
    return render_template("index.html", udp_port=UDP_PORT)

@app.get("/api/latest")
def api_latest() -> Response:
    since = float(request.args.get("since", 0))
    limit = int(request.args.get("limit", 300))
    with _lock:
        samples = [s for s in _history if s["t"] > since]
        stats = dict(_stats)
    if limit:
        samples = samples[-limit:]
    return jsonify({"samples": samples, "stats": stats})

@app.post("/api/record/start")
def record_start() -> Response:
    global _csv_file, _csv_writer
    with _lock:
        if _stats["recording"]:
            return jsonify({"ok": False, "error": "already_recording"})
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = _stats["last_uid"] or "unknown"
        path = DATA_DIR / f"imu_{uid}_{ts}.csv"
        _csv_file = open(path, "w", newline="", encoding="utf-8")
        _csv_writer = csv.writer(_csv_file)
        _csv_writer.writerow(["timestamp", "seq", "ax", "ay", "az", "gx", "gy", "gz"])
        _stats["recording"] = True
        _stats["csv_path"] = str(path)
        _stats["csv_rows"] = 0
    return jsonify({"ok": True, "path": str(path)})

@app.post("/api/record/stop")
def record_stop() -> Response:
    global _csv_file, _csv_writer
    with _lock:
        if not _stats["recording"]:
            return jsonify({"ok": False, "error": "not_recording"})
        _stats["recording"] = False
        path = _stats["csv_path"]
        rows = _stats["csv_rows"]
        _csv_writer = None
        if _csv_file:
            _csv_file.close()
            _csv_file = None
    return jsonify({"ok": True, "path": path, "rows": rows})

@app.get("/api/stats")
def api_stats() -> Response:
    with _lock:
        return jsonify(dict(_stats))

@app.get("/api/recordings")
def api_recordings() -> Response:
    files = []
    if DATA_DIR.exists():
        for f in sorted(DATA_DIR.glob("*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = f.stat()
            files.append({
                "name": f.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return jsonify({"recordings": files, "dir": str(DATA_DIR)})

@app.get("/api/net/status")
def net_status() -> Response:
    try:
        gw = _gw_request("GET", "/api/status")
    except Exception as exc:
        return jsonify({"ok": False, "error": "gateway_unreachable", "detail": str(exc)}), 502
    devices = (gw.get("state") or {}).get("devices") or []
    if isinstance(devices, dict):
        devices = list(devices.values())
    dev = next(
        (d for d in devices if str(d.get("device_uid") or "").upper() == DEVICE_UID.upper()),
        None,
    )
    with _lock:
        uid = _stats["last_uid"]
        rx  = _stats["last_rx_ms"]
    live = uid.upper() == DEVICE_UID.upper() and rx > 0 and (time.time() * 1000 - rx) < 5000
    if dev is None:
        return jsonify({"ok": False, "error": "device_not_found", "viewer_live": live}), 404
    return jsonify({
        "ok":               True,
        "uid":              DEVICE_UID,
        "viewer_live":      live,
        "mode":             dev.get("mode"),
        "wifi_rssi":        dev.get("wifi_rssi"),
        "last_heartbeat_at": dev.get("last_heartbeat_at"),
        "transport_path":   dev.get("transport_path"),
        "peer":             dev.get("peer"),
    })


@app.post("/api/net/wifi")
def net_wifi() -> Response:
    body     = request.get_json(silent=True) or {}
    ssid     = str(body.get("ssid") or "").strip()
    password = str(body.get("password") or "")
    if not ssid:
        return jsonify({"ok": False, "error": "ssid_required"}), 400
    try:
        r1 = _gw_request(
            "POST", f"/api/devices/{DEVICE_UID}/cmd",
            {"command": "set_wifi", "ssid": ssid, "password": password},
        )
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": "device_unreachable", "detail": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": "gateway_unreachable", "detail": str(exc)}), 502
    if not r1.get("ok"):
        return jsonify({"ok": False, "error": "set_wifi_failed", "set_wifi": r1}), 502
    time.sleep(1)
    try:
        r2 = _gw_request("POST", f"/api/devices/{DEVICE_UID}/cmd", {"command": "reboot"})
    except Exception as exc:
        return jsonify({"ok": False, "error": "reboot_failed", "set_wifi": r1, "detail": str(exc)}), 502
    return jsonify({"ok": True, "set_wifi": r1, "reboot": r2})


@app.post("/api/net/rediscover")
def net_rediscover() -> Response:
    try:
        r1 = _gw_request("POST", "/api/discover")
    except Exception as exc:
        return jsonify({"ok": False, "error": "gateway_unreachable", "detail": str(exc)}), 502
    time.sleep(5)
    try:
        r2 = _gw_request("POST", f"/api/devices/{DEVICE_UID}/serve")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if hasattr(exc, "read") else str(exc)
        return jsonify({"ok": False, "error": "serve_failed", "discover": r1, "detail": raw}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": "serve_failed", "discover": r1, "detail": str(exc)}), 502
    ok = bool(r2.get("ok"))
    return jsonify({"ok": ok, "discover": r1, "serve": r2}), (200 if ok else 502)


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    t = threading.Thread(target=_udp_worker, daemon=True)
    t.start()
    print(f"IMU viewer: web UI at http://127.0.0.1:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
