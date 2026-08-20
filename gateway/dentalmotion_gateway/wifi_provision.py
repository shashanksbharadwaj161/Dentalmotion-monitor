from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any

IS_WINDOWS = sys.platform == "win32"
BOARD_SETUP_PREFIX = "DentalMotion-Setup-"
BOARD_PORTAL_SAVE_URL = "http://192.168.4.1/save"


class ProvisioningError(Exception):
    pass


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    # netsh's console output uses the Windows OEM codepage, which does not
    # always match Python's default text-mode decoding (especially with
    # non-ASCII SSIDs nearby) — decode leniently instead of raising.
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    raw = result.stdout or b""
    try:
        import locale
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(encoding, errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _first_wifi_interface_name() -> str:
    out = _run(["netsh", "wlan", "show", "interfaces"])
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name") and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def get_current_wifi() -> dict[str, str]:
    if not IS_WINDOWS:
        return {"interface": "", "ssid": ""}
    out = _run(["netsh", "wlan", "show", "interfaces"])
    ssid = ""
    interface = ""
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Name":
            interface = value
        elif key == "SSID":
            ssid = value
    return {"interface": interface, "ssid": ssid}


def find_setup_networks(interface: str) -> list[str]:
    if not IS_WINDOWS:
        return []
    out = _run(["netsh", "wlan", "show", "networks", f"interface={interface}"])
    found: list[str] = []
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line.startswith("SSID"):
            continue
        _, _, value = line.partition(":")
        name = value.strip()
        if name.startswith(BOARD_SETUP_PREFIX) and name not in found:
            found.append(name)
    return found


def _connect_open_network(interface: str, ssid: str) -> None:
    profile_xml = (
        '<?xml version="1.0"?>'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
        f"<name>{ssid}</name>"
        "<SSIDConfig><SSID>"
        f"<name>{ssid}</name>"
        "</SSID></SSIDConfig>"
        "<connectionType>ESS</connectionType>"
        "<connectionMode>manual</connectionMode>"
        "<MSM><security><authEncryption>"
        "<authentication>open</authentication>"
        "<encryption>none</encryption>"
        "<useOneX>false</useOneX>"
        "</authEncryption></security></MSM>"
        "</WLANProfile>"
    )
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile_xml)
        _run(["netsh", "wlan", "add", "profile", f"filename={path}", f"interface={interface}", "user=current"])
        _run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={interface}"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _wait_for_ssid(interface: str, expected_ssid: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_current_wifi().get("ssid") == expected_ssid:
            return True
        time.sleep(0.5)
    return False


def _post_credentials(ssid: str, password: str, timeout: float = 8.0) -> None:
    data = urllib.parse.urlencode({"ssid": ssid, "password": password}).encode("utf-8")
    request_obj = urllib.request.Request(BOARD_PORTAL_SAVE_URL, data=data, method="POST")
    with urllib.request.urlopen(request_obj, timeout=timeout) as response:
        response.read()


def provision_board(target_ssid: str, target_password: str, board_ap_ssid: str | None = None) -> dict[str, Any]:
    """Join a board's open setup hotspot, hand it WiFi credentials, then
    return this machine's WiFi to whatever it was connected to before.

    Windows-only: uses `netsh wlan` to switch networks. On other platforms
    this raises ProvisioningError so the caller can fall back to the manual
    (phone/laptop visits 192.168.4.1) flow.
    """
    if not IS_WINDOWS:
        raise ProvisioningError("automatic_wifi_switch_only_supported_on_windows")

    original = get_current_wifi()
    interface = original.get("interface") or _first_wifi_interface_name()
    if not interface:
        raise ProvisioningError("no_wifi_interface_found")

    try:
        target_board_ssid = board_ap_ssid
        if not target_board_ssid:
            # Some WiFi adapter drivers (seen on this dev machine's Realtek
            # USB dongle) skip background scanning while already associated
            # to a network, silently returning a stale/empty network list. A
            # fresh scan only reliably surfaces new networks (like the
            # board's open setup AP) right after disconnecting.
            _run(["netsh", "wlan", "disconnect", f"interface={interface}"])
            time.sleep(1.5)
            candidates = find_setup_networks(interface)
            if not candidates:
                raise ProvisioningError("no_setup_network_found")
            target_board_ssid = candidates[0]

        _connect_open_network(interface, target_board_ssid)
        if not _wait_for_ssid(interface, target_board_ssid, timeout=15.0):
            raise ProvisioningError("failed_to_join_board_network")
        time.sleep(1.0)
        _post_credentials(target_ssid, target_password)
    finally:
        original_ssid = original.get("ssid")
        if original_ssid:
            try:
                _run(["netsh", "wlan", "connect", f"name={original_ssid}", f"interface={interface}"])
            except Exception:
                pass

    return {"ok": True, "board_ssid": target_board_ssid, "target_ssid": target_ssid}
