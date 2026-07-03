#!/usr/bin/env python3
"""Cross-platform Gateway start script. Usage: python scripts/start.py"""
import os
import sys
import time
import shutil
import socket
import subprocess
from pathlib import Path

APP_DIR    = Path(__file__).resolve().parent.parent
RUN_DIR    = APP_DIR / ".run"
PID_FILE   = RUN_DIR / "gateway.pid"
LOG_FILE   = RUN_DIR / "gateway.log"
CONFIG_FILE = RUN_DIR / "config.json"
VENV       = APP_DIR / ".venv"

IS_WIN = sys.platform == "win32"


def _venv_bin(name):
    return VENV / ("Scripts" if IS_WIN else "bin") / (name + (".exe" if IS_WIN else ""))


def _stop_previous():
    if not PID_FILE.exists():
        return
    pid_text = PID_FILE.read_text().strip()
    if pid_text:
        try:
            pid = int(pid_text)
            if IS_WIN:
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, 15)
            print(f"Stopped previous instance (PID {pid}).")
            time.sleep(1)
        except (ValueError, ProcessLookupError, OSError):
            pass
    PID_FILE.unlink(missing_ok=True)


def _check_ports():
    for proto, port in [("UDP", 22346), ("UDP", 13250), ("TCP", 5052)]:
        kind = socket.SOCK_DGRAM if proto == "UDP" else socket.SOCK_STREAM
        with socket.socket(socket.AF_INET, kind) as s:
            try:
                s.bind(("", port))
            except OSError:
                print(f"ERROR: {proto}/{port} is already in use.", file=sys.stderr)
                sys.exit(1)


def _create_venv():
    if not _venv_bin("python").exists():
        print("Creating .venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)


def _install_deps():
    pip = str(_venv_bin("pip"))
    subprocess.run([pip, "install", "-q", "-r", str(APP_DIR / "requirements.txt")], check=True)
    subprocess.run([pip, "install", "-q", "-e", str(APP_DIR)], check=True)


def _create_config():
    if not CONFIG_FILE.exists():
        shutil.copy(APP_DIR / "config.example.json", CONFIG_FILE)
        print(f"Created {CONFIG_FILE} - open the WebUI to finish setup.")


def _launch():
    python = str(_venv_bin("python"))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    env["NEWHORIZONS_GATEWAY_APP_ROOT"] = str(APP_DIR)
    env["NEWHORIZONS_GATEWAY_RESTART_COMMAND"] = f"{sys.executable} {APP_DIR / 'scripts' / 'start.py'}"

    with open(LOG_FILE, "w") as log:
        kwargs = dict(
            stdout=log, stderr=log,
            env=env,
            cwd=str(APP_DIR),
        )
        if IS_WIN:
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [python, "-m", "newhorizons_gateway.main", "--config", str(CONFIG_FILE)],
            **kwargs,
        )

    PID_FILE.write_text(str(proc.pid))
    time.sleep(1)

    if proc.poll() is not None:
        print("ERROR: Gateway failed to start.", file=sys.stderr)
        print(LOG_FILE.read_text(errors="replace")[:2000], file=sys.stderr)
        sys.exit(1)

    print(f"Gateway started   PID {proc.pid}")
    print("WebUI             http://127.0.0.1:5052")
    print(f"Log               {LOG_FILE}")


def main():
    RUN_DIR.mkdir(exist_ok=True)
    _stop_previous()
    _check_ports()
    _create_venv()
    _install_deps()
    _create_config()
    _launch()


if __name__ == "__main__":
    main()
