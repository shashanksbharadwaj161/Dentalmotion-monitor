"""
DentalMotion Monitor – Local IMU Viewer
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
UDP_PORT = int(os.environ.get("DENTALMOTION_UDP_PORT", 13253))   # mirror port from gateway
WEB_PORT = int(os.environ.get("DENTALMOTION_WEB_PORT", 8765))
DATA_DIR     = Path.home() / "Desktop" / "DentalMotion_Recordings"
GATEWAY_URL  = os.environ.get("DENTALMOTION_GATEWAY_URL", "http://127.0.0.1:5052")
DEVICE_UID   = os.environ.get("DENTALMOTION_DEVICE_UID",  "3CDC75413DE8")
HISTORY_SECONDS = 15
MAX_HISTORY = 1500    # 100 fps × 15 s

# ── DentalMotion packet constants ─────────────────────────────────────────────────────
MAGIC = 0xA55A
HEADER_LEN = 20
FLAG_IMU = 0x01
FLAG_HEARTBEAT = 0x80
IMU_FLOATS = 7   # ax ay az gx gy gz 0(reserved)

# ── Shared state ─────────────────────────────────────────────────────────────
# Per-device state, keyed by device UID — lets multiple boards stream
# simultaneously without their samples getting interleaved into one stream.
_lock = threading.Lock()
_devices: dict[str, dict[str, Any]] = {}       # uid -> {history, stats, csv_file, csv_writer}
_device_order: list[str] = []                   # first-seen order, for "Board 1"/"Board 2" labels


def _device(uid: str) -> dict[str, Any]:
    """Get or create per-device state. Caller must hold _lock."""
    dev = _devices.get(uid)
    if dev is None:
        dev = {
            "history": deque(maxlen=MAX_HISTORY),
            "stats": {
                "packets_received": 0,
                "imu_samples": 0,
                "last_uid": uid,
                "last_rx_ms": 0,
                "recording": False,
                "csv_path": "",
                "csv_rows": 0,
            },
            "csv_file": None,
            "csv_writer": None,
        }
        _devices[uid] = dev
        _device_order.append(uid)
    return dev


DEVICE_LABELS = ["Yellow", "Pink", "Blue", "Green", "Orange", "Purple"]


def _label_for(uid: str) -> str:
    """Deterministic per-UID label — the same physical board always gets the
    same color, regardless of connection order or how many times the app
    restarts. (Not Python's built-in hash(): that's randomized per-process
    unless PYTHONHASHSEED is fixed, which would make labels flip on every
    restart — exactly what we're avoiding.)"""
    checksum = sum(uid.encode("ascii", errors="replace"))
    return DEVICE_LABELS[checksum % len(DEVICE_LABELS)]

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
            continue  # unparseable / heartbeat-only packet, no device UID to attribute it to

        with _lock:
            dev = _device(sample["uid"])
            stats = dev["stats"]
            stats["packets_received"] += 1
            stats["imu_samples"] += 1
            stats["last_uid"] = sample["uid"]
            stats["last_rx_ms"] = int(time.time() * 1000)
            dev["history"].append(sample)
            if stats["recording"] and dev["csv_writer"] is not None:
                dev["csv_writer"].writerow([
                    datetime.fromtimestamp(sample["t"]).isoformat(),
                    sample["seq"],
                    sample["ax"], sample["ay"], sample["az"],
                    sample["gx"], sample["gy"], sample["gz"],
                ])
                dev["csv_file"].flush()
                stats["csv_rows"] += 1

# ── Gateway helper ────────────────────────────────────────────────────────────

def _gw_request(method: str, path: str, body: dict | None = None, timeout: float = 6) -> dict:
    url  = GATEWAY_URL + path
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    hdrs: dict[str, str] = {"Content-Type": "application/json"} if data else {}
    req  = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.get("/")
def index() -> str:
    return render_template("index.html", udp_port=UDP_PORT)

def _selected_uid(args) -> str | None:
    """Resolve which device a request is about: explicit ?uid= wins, else the
    first live (recently-seen) device, else the first known device at all."""
    uid = args.get("uid")
    if uid:
        return uid.upper()
    with _lock:
        now_ms = time.time() * 1000
        for u in _device_order:
            stats = _devices[u]["stats"]
            if stats["last_rx_ms"] and now_ms - stats["last_rx_ms"] < 5000:
                return u
        return _device_order[0] if _device_order else None


@app.get("/api/devices")
def api_devices() -> Response:
    """List every board this viewer has ever seen a packet from, in the
    order first seen — that order is what "Board 1" / "Board 2" means."""
    with _lock:
        now_ms = time.time() * 1000
        out = []
        for uid in _device_order:
            stats = _devices[uid]["stats"]
            live = bool(stats["last_rx_ms"]) and (now_ms - stats["last_rx_ms"]) < 5000
            out.append({
                "uid": uid,
                "label": _label_for(uid),
                "live": live,
                "last_rx_ms": stats["last_rx_ms"],
                "recording": stats["recording"],
            })
    return jsonify({"devices": out})


@app.get("/api/latest")
def api_latest() -> Response:
    since = float(request.args.get("since", 0))
    limit = int(request.args.get("limit", 300))
    uid = _selected_uid(request.args)
    with _lock:
        if uid is None:
            samples: list[dict[str, Any]] = []
            stats = {
                "packets_received": 0, "imu_samples": 0, "last_uid": "",
                "last_rx_ms": 0, "recording": False, "csv_path": "", "csv_rows": 0,
            }
        else:
            dev = _device(uid)
            samples = [s for s in dev["history"] if s["t"] > since]
            stats = dict(dev["stats"])
    if limit:
        samples = samples[-limit:]
    return jsonify({"samples": samples, "stats": stats, "uid": uid, "label": _label_for(uid) if uid else None})

@app.post("/api/record/start")
def record_start() -> Response:
    body = request.get_json(silent=True) or {}
    uid = (body.get("uid") or request.args.get("uid") or "").upper() or _selected_uid(request.args)
    if not uid:
        return jsonify({"ok": False, "error": "no_device"})
    with _lock:
        dev = _device(uid)
        stats = dev["stats"]
        if stats["recording"]:
            return jsonify({"ok": False, "error": "already_recording"})
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DATA_DIR / f"imu_{uid}_{ts}.csv"
        dev["csv_file"] = open(path, "w", newline="", encoding="utf-8")
        dev["csv_writer"] = csv.writer(dev["csv_file"])
        dev["csv_writer"].writerow(["timestamp", "seq", "ax", "ay", "az", "gx", "gy", "gz"])
        stats["recording"] = True
        stats["csv_path"] = str(path)
        stats["csv_rows"] = 0
    return jsonify({"ok": True, "path": str(path), "uid": uid})

@app.post("/api/record/stop")
def record_stop() -> Response:
    body = request.get_json(silent=True) or {}
    uid = (body.get("uid") or request.args.get("uid") or "").upper() or _selected_uid(request.args)
    if not uid:
        return jsonify({"ok": False, "error": "no_device"})
    with _lock:
        dev = _device(uid)
        stats = dev["stats"]
        if not stats["recording"]:
            return jsonify({"ok": False, "error": "not_recording"})
        stats["recording"] = False
        path = stats["csv_path"]
        rows = stats["csv_rows"]
        dev["csv_writer"] = None
        if dev["csv_file"]:
            dev["csv_file"].close()
            dev["csv_file"] = None
    return jsonify({"ok": True, "path": path, "rows": rows})

@app.get("/api/stats")
def api_stats() -> Response:
    uid = _selected_uid(request.args)
    with _lock:
        if uid is None:
            return jsonify({})
        return jsonify(dict(_device(uid)["stats"]))

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
    uid = (request.args.get("uid") or "").upper() or DEVICE_UID
    try:
        gw = _gw_request("GET", "/api/status")
    except Exception as exc:
        return jsonify({"ok": False, "error": "gateway_unreachable", "detail": str(exc)}), 502
    devices = (gw.get("state") or {}).get("devices") or []
    if isinstance(devices, dict):
        devices = list(devices.values())
    dev = next(
        (d for d in devices if str(d.get("device_uid") or "").upper() == uid.upper()),
        None,
    )
    with _lock:
        stats = _devices.get(uid, {}).get("stats") or {}
        rx = stats.get("last_rx_ms", 0)
    live = rx > 0 and (time.time() * 1000 - rx) < 5000
    if dev is None:
        return jsonify({"ok": False, "error": "device_not_found", "viewer_live": live, "uid": uid}), 404
    return jsonify({
        "ok":               True,
        "uid":              uid,
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
    uid      = str(body.get("uid") or "").upper() or DEVICE_UID
    if not ssid:
        return jsonify({"ok": False, "error": "ssid_required"}), 400
    try:
        r1 = _gw_request(
            "POST", f"/api/devices/{uid}/cmd",
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
        r2 = _gw_request("POST", f"/api/devices/{uid}/cmd", {"command": "reboot"})
    except Exception as exc:
        return jsonify({"ok": False, "error": "reboot_failed", "set_wifi": r1, "detail": str(exc)}), 502
    return jsonify({"ok": True, "set_wifi": r1, "reboot": r2})


@app.post("/api/net/rediscover")
def net_rediscover() -> Response:
    body = request.get_json(silent=True) or {}
    uid = str(body.get("uid") or request.args.get("uid") or "").upper() or DEVICE_UID
    try:
        r1 = _gw_request("POST", "/api/discover")
    except Exception as exc:
        return jsonify({"ok": False, "error": "gateway_unreachable", "detail": str(exc)}), 502
    time.sleep(5)
    try:
        r2 = _gw_request("POST", f"/api/devices/{uid}/serve")
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
