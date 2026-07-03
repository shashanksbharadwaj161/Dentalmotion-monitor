#!/usr/bin/env python3
"""Cross-platform Gateway stop script. Usage: python scripts/stop.py"""
import os
import sys
import subprocess
from pathlib import Path

APP_DIR  = Path(__file__).resolve().parent.parent
PID_FILE = APP_DIR / ".run" / "gateway.pid"

IS_WIN = sys.platform == "win32"


def main():
    if not PID_FILE.exists():
        print("Gateway is not running (no PID file).")
        return

    pid_text = PID_FILE.read_text().strip()
    if pid_text:
        try:
            pid = int(pid_text)
            if IS_WIN:
                result = subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                print(f"Gateway stopped (PID {pid})." if result == 0 else "Gateway is not running.")
            else:
                os.kill(pid, 15)
                print(f"Gateway stopped (PID {pid}).")
        except (ValueError, ProcessLookupError, OSError):
            print("Gateway is not running.")

    PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
