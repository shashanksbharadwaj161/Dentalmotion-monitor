# DentalMotion Monitor firmware

Minimal, custom Arduino firmware for the DentalMotion Monitor's ESP32-S3
board. IMU-only wrist motion monitor — no pressure matrix, no OTA, no
display, no LEDs, no battery-charger management, no safe-mode/boot-counter
logic, no cloud dependency. Speaks a small UDP/JSON wire protocol to
`gateway/` (see `arduino_protocol.py`, `discovery.py`, `udp_control.py`).

Verified to compile clean (0 warnings/errors) and boot correctly on real
hardware (ESP32-S3, rev v0.2, GigaDevice 8MB flash) against `esp32:esp32`
core **2.0.9** with `Arduino_BMI270_BMM150` 1.2.3 and `ArduinoJson` 6.21.5 —
754373 bytes (22%) flash, 46128 bytes (14%) RAM.

> **Core version matters — do not use esp32:esp32 3.x on this board.**
> Building against core 3.3.4 (or presumably any 3.x) compiles cleanly and
> flashes cleanly, but the board panics on every boot with a
> `Guru Meditation Error: Cache error / MMU entry fault` before any
> application code runs — confirmed by testing on this exact physical unit.
> Pin the core to `2.0.9` (`arduino-cli core install esp32:esp32@2.0.9`,
> which requires adding Espressif's package index —
> `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
> — since recent `arduino-cli` installs default to 3.x only).

## Build

```
arduino-cli config set board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32@2.0.9
arduino-cli lib install "Arduino_BMI270_BMM150" "ArduinoJson@6.21.5"
arduino-cli compile --fqbn esp32:esp32:esp32s3:FlashSize=8M,PartitionScheme=default_8MB \
  --build-path build firmware/dentalmotion_board --output-dir out
```

> Pin `ArduinoJson` to the 6.x line. ArduinoJson 7 needs a newer C++ standard
> than the esp32 core 2.0.9 toolchain defaults to (`gnu++11`) and will fail
> to compile with confusing, mis-located parser errors.

## Flash

The board's auto-reset (DTR/RTS) does not work. Before every flash, put it
in download mode manually: flip the power **slide switch OFF**, hold
**BOOT**, flip the slide switch **ON** while still holding BOOT, keep
holding ~3s, then release.

> On this board, unplugging/replugging the USB cable alone does **not**
> reset the ESP32-S3 chip — only the slide switch does. USB power and the
> chip's own power rail are separate. If `esptool` reports
> `Wrong boot mode detected (0x14)`, you didn't power-cycle via the switch.

```
python -m esptool --chip esp32s3 --port COMxx --baud 460800 \
  --before default-reset --after hard-reset write-flash -z \
  0x0     out/dentalmotion_board.ino.bootloader.bin \
  0x8000  out/dentalmotion_board.ino.partitions.bin \
  0x10000 out/dentalmotion_board.ino.bin
```

## First boot / changing WiFi

On first boot (or if stored credentials fail to connect), the board starts a
SoftAP named `DentalMotion-Setup-<12-hex-MAC>` at `192.168.4.1`. Join it with a
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
