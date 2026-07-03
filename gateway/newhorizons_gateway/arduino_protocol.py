from __future__ import annotations

import json
import struct
from typing import Any


PROTOCOL = "NHO/Arduino/1"
CONTROL_PORT = 22345
PACKET_MAGIC = 0xA55A
PACKET_VERSION = 3
PACKET_FLAG_HEARTBEAT = 0x80
HEADER_LEN = 20
HEADER_PREFIX = struct.Struct("<HBB")
HEADER_TAIL = struct.Struct("<IIH")


def encode_command_line(payload: dict[str, Any]) -> bytes:
    message = {"protocol": PROTOCOL}
    message.update(dict(payload or {}))
    return (json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode_json_line(payload: bytes | bytearray | str, *, require_protocol: bool = True) -> dict[str, Any]:
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8")
    else:
        text = str(payload)
    data = json.loads(text.strip())
    if require_protocol and data.get("protocol") != PROTOCOL:
        raise ValueError("unsupported_protocol")
    return data


def is_arduino_stream_packet(payload: bytes | bytearray) -> bool:
    packet = bytes(payload)
    if len(packet) < HEADER_LEN:
        return False
    try:
        magic, version, _flags = HEADER_PREFIX.unpack_from(packet, 0)
        _frame_id, _timestamp_ms, payload_len = HEADER_TAIL.unpack_from(packet, 10)
    except struct.error:
        return False
    return magic == PACKET_MAGIC and version == PACKET_VERSION and len(packet) >= HEADER_LEN + int(payload_len)


def is_arduino_heartbeat_packet(payload: bytes | bytearray) -> bool:
    packet = bytes(payload)
    if len(packet) < HEADER_LEN:
        return False
    try:
        magic, version, flags = HEADER_PREFIX.unpack_from(packet, 0)
        _frame_id, _timestamp_ms, payload_len = HEADER_TAIL.unpack_from(packet, 10)
    except struct.error:
        return False
    return (
        magic == PACKET_MAGIC
        and version == PACKET_VERSION
        and bool(flags & PACKET_FLAG_HEARTBEAT)
        and int(payload_len) == 0
        and len(packet) >= HEADER_LEN
    )


def packet_device_uid(payload: bytes | bytearray) -> str:
    packet = bytes(payload)
    if not is_arduino_stream_packet(packet):
        return ""
    return packet[4:10].hex().upper()
