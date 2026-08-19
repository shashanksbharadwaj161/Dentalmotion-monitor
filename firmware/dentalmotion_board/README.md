# DentalMotion Monitor firmware

Minimal, custom Arduino firmware for the DentalMotion Monitor's ESP32-S3
board. IMU-only wrist motion monitor — no pressure matrix, no OTA, no
display, no LEDs, no battery-charger management, no safe-mode/boot-counter
logic. It speaks the exact same UDP wire protocol as the old New Horizons OS
firmware, so it's a drop-in replacement: `gateway/` and `imu_viewer/` need no
changes.

Verified to compile clean (0 warnings/errors) against `esp32:esp32` core 2.0.9
with `Arduino_BMI270_BMM150` 1.2.3 and `ArduinoJson` 6.21.5 — 758233 bytes
(22%) flash, 46472 bytes (14%) RAM.

## Build

```
arduino-cli lib install "Arduino_BMI270_BMM150" "ArduinoJson@6.21.5"
arduino-cli compile --fqbn esp32:esp32:esp32s3:FlashSize=8M,PartitionScheme=default_8MB \
  --build-path build firmware/dentalmotion_board --output-dir out
```

> Pin `ArduinoJson` to the 6.x line. ArduinoJson 7 needs a newer C++ standard
> than the esp32 core 2.0.9 toolchain defaults to (`gnu++11`) and will fail
> to compile with confusing, mis-located parser errors. If you're on a newer
> esp32 core (3.x, which defaults to a newer C++ standard) ArduinoJson 7
> should be fine too — just verify with a clean build either way.

## Flash

The board's auto-reset (DTR/RTS) does not work. Before every flash:
hold **BOOT**, unplug/replug USB, hold ~2s, release.

```
python -m esptool --chip esp32s3 --port COMxx --baud 460800 \
  --before default-reset --after hard-reset write-flash -z \
  0x0     out/dentalmotion_board.ino.bootloader.bin \
  0x8000  out/dentalmotion_board.ino.partitions.bin \
  0x10000 out/dentalmotion_board.ino.bin
```

## First boot / changing WiFi

On first boot (or if stored credentials fail to connect), the board starts a
SoftAP named `NewHorizonsOS-<12-hex-MAC>` at `192.168.4.1`. Join it with a
phone or laptop — a captive-portal setup page should pop up automatically
(or open `http://192.168.4.1/`). Pick or type the SSID, enter the password,
submit — the board saves the credentials and reboots onto that network.

WiFi can also be changed later from the dashboard's **Apply & Reboot Board**
button (`imu_viewer` → gateway → board's UDP control channel), with no
reflashing needed.

## Verify

1. Power on — the setup AP should appear if no WiFi is stored yet.
2. After WiFi setup, check the gateway's `/api/status`: the device should
   show `findme_state: "attached"` with `udp_packets` climbing.
3. Open the dashboard — the 3D view and accel/gyro graphs should update live
   (roughly 56–100 packets/s is the expected/normal range).
