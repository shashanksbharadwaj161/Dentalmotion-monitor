# DentalMotion Monitor — Native Android App: Build Plan

**Purpose of this document:** a self-contained plan for building a native Android
app that replaces the laptop/PC gateway entirely — the phone talks directly to
the sensor board over WiFi, with no separate server device. Written to be
handed to a fresh Claude session (in a separate repo) with no other context
from the parent project.

**Source of truth:** this repo — `shashanksbharadwaj161/Dentalmotion-monitor` —
is the reference implementation. Its firmware (`firmware/dentalmotion_board/dentalmotion_board.ino`)
and gateway (`gateway/dentalmotion_gateway/`) are the *only* correct
description of the wire protocol. If anything here conflicts with that
source, the source wins — pull the current version before starting.

---

## 1. Why this exists / what it replaces

Today: `[ESP32-S3 board] --WiFi(UDP)--> [Python gateway, must run on a PC] --HTTP--> [browser dashboard]`.
Two known-bad alternatives, ruled out on this exact hardware:

- **Bluetooth** — an ESP32 BLE-provisioning attempt on this board crash-looped
  every single boot (`assert failed: block_trim_free heap_tlsf.c:371`, heap
  corruption in the Bluetooth controller init, on `esp32:esp32` core 2.0.9).
  Reproduced 6+ times back to back on real hardware. Treat any Bluetooth path
  on this specific board+toolchain as unproven and likely broken until
  demonstrated otherwise on real hardware.
- **Cloud hosting** (Vercel/Render/etc.) — architecturally impossible. The
  discovery step is a UDP *broadcast* (`255.255.255.255`), which never
  leaves the local subnet/router. A cloud server cannot receive it, period.

Target: `[ESP32-S3 board] --WiFi(UDP)--> [Android app]`. The phone becomes
the gateway and the viewer in one process. No PC, no Pi, nothing else.

This is real native development, not a config change — budget actual time
for it (see §7, phased milestones).

---

## 2. Hard technical constraints (read before writing any code)

1. **This must be a native app, not a web app / PWA.** Browsers cannot open
   raw UDP sockets — there is no web API for it. A WebView-based app is a
   dead end for the actual data path.
2. **Android drops broadcast/multicast UDP packets by default** to save
   battery. You must acquire a `WifiManager.MulticastLock` before the
   discovery socket will reliably receive anything, and hold it for the
   life of the listening session. This is the single most common reason a
   first attempt silently receives nothing.
3. **Android kills background network activity aggressively** (Doze /
   App Standby). If continuous reception while the app is not in the
   foreground is required, it must run as a proper foreground service with
   a persistent notification and the relevant battery-optimization
   exemption requested from the user. For a v1, treat this as out of scope
   and only guarantee reception while the app is open and foregrounded —
   call this out explicitly in the UI rather than silently failing later.
4. **All networking must happen off the main thread.** Standard Android
   rule (`NetworkOnMainThreadException`), but worth stating since the UDP
   receive loop is a tight, continuous operation — use a dedicated thread
   or coroutine + `Dispatchers.IO`, not `AsyncTask`.
5. **Do not attempt to replicate the Windows-only auto WiFi-join feature.**
   The current gateway has a `netsh`-based "computer joins the board's
   setup hotspot automatically" feature (`gateway/dentalmotion_gateway/wifi_provision.py`).
   This has no Android equivalent worth building — Android's WiFi APIs for
   programmatically joining arbitrary networks are locked down (and get
   more restrictive with each OS version). Design for the **manual** flow
   instead (§5.1) — it already works today, requires no new code, and
   matches how a phone naturally joins a hotspot.
6. **No firmware changes of any kind.** The board's firmware is fixed,
   tested, and working. This app is a from-scratch *client* of an existing,
   frozen protocol.

---

## 3. Wire protocol — exact, byte-for-byte

Pulled directly from `firmware/dentalmotion_board/dentalmotion_board.ino`
and cross-checked against `gateway/dentalmotion_gateway/arduino_protocol.py`
and `discovery.py` in the reference repo. This is the single most
important section — get this wrong and nothing works.

### 3.1 Ports

| Port  | Protocol | Direction | Purpose |
|-------|----------|-----------|---------|
| 22346 | UDP broadcast | board → app, app → board | FindMe discovery/offer/probe (JSON) |
| 13250 | UDP unicast | board → app (data), app → board (commands) | Binary sensor stream + heartbeat; JSON control channel |

Both ports must be bound by the app to *receive* on them. The board
broadcasts on 22346 to `255.255.255.255` and to the current subnet's
broadcast address; it doesn't know the app's IP in advance — that's the
whole point of "discovery."

### 3.2 Binary packet header (20 bytes, all multi-byte fields little-endian)

Written by `writeHeader()` in the firmware:

```
Offset  Size  Field
0       2     magic          = 0xA55A  (bytes on the wire: 0x5A, 0xA5)
2       1     version        = 3
3       1     flags          FLAG_IMU=0x01, FLAG_HEARTBEAT=0x80
4       6     mac_bytes      board's WiFi MAC (also = its 12-hex-char device UID)
10      4     seq            uint32, increments per packet sent by the board
14      4     timestamp_ms   uint32, board's millis() at send time
18      2     payload_len    uint16, length of the payload that follows
```

Two packet kinds ride on this header, both sent to whatever `(gatewayIp,
gatewayPort)` the board learned from the FindMe offer (§3.3):

- **IMU packet**: `flags = 0x01`, `payload_len = 28`. Payload is **7
  little-endian float32s**: `ax, ay, az, gx, gy, gz, reserved(always 0.0)`.
  Total packet size = 20 + 28 = 48 bytes. Sent at ~100 Hz (every 10 ms) while
  attached.
- **Heartbeat packet**: `flags = 0x80`, `payload_len = 0`. Total size = 20
  bytes, no payload. Sent every 3000 ms while attached. Liveness signal
  only — don't expect sensor data in it.

Units: acceleration in g (not m/s²), gyro in degrees/second (not rad/s) —
confirmed by the existing dashboard's direct display of these values
without conversion.

### 3.3 FindMe discovery (JSON over UDP, port 22346)

**Board → app, broadcast, every 3000 ms while not attached** (`sendFindmeDiscover`):

```json
{
  "type": "findme_discover",
  "device_uid": "3CDC75413DE8",
  "device_name": "DentalMotion Monitor-3CDC75413DE8",
  "mode": "normal",
  "firmware_version": "v1.0.0",
  "hardware_model": "DentalMotion-ESP32S3 v1.0",
  "wifi_rssi": -44,
  "protocol": "DentalMotion/1",
  "current_gateway_id": "..."   // omitted if not currently attached anywhere
}
```

`device_uid` is the canonical identifier throughout the whole system — it's
just the 6 MAC bytes as 12 uppercase hex chars, no separators.

**App → board, unicast reply to the discover packet's source address**
(mirrors `discovery.py`'s `_offer_payload`):

```json
{
  "type": "findme_offer",
  "device_uid": "3CDC75413DE8",
  "accept": true,
  "udp_port": 13250,
  "gateway_id": "android-app-<something-stable>",
  "version": 1,
  "priority": 100,
  "upstream_status": "online",
  "ttl_ms": 10000,
  "server_time": 1735689600000
}
```

Only `type`, `device_uid`, `accept`, and `udp_port` are strictly required
for the board to lock on (see firmware's `serviceFindme()`: it reads
exactly these four fields and ignores the rest). Send the others anyway
for forward compatibility with the reference gateway's expectations.
**`device_uid` must match exactly** (`strcasecmp`, so case-insensitive, but
match it exactly regardless) or the board silently ignores the offer.
Once accepted, the board sets `gatewayIp` = the offer's source IP and
`gatewayPort` = the offer's `udp_port`, and starts streaming to exactly
that address — nowhere else.

**Re-discovery ("probe")** — if you need an *already-attached* board to
re-announce itself (e.g., the app restarted and lost its offer state),
broadcast:

```json
{ "type": "findme_probe", "gateway_id": "...", "gateway_name": "...", "udp_port": 13250 }
```

The board replies with a fresh `findme_discover` (including
`current_gateway_id` if it's attached elsewhere) rather than switching
gateways on its own — the existing gateway's `discovery.py` deliberately
never "steals" a device that's already attached to a different gateway
except via an explicit claim flow the v1 Android app doesn't need to
implement.

### 3.4 Control channel (JSON over UDP, port 13250 — same port as the binary stream)

This is bidirectional on the *same* socket the board uses to send you IMU
data. Don't open a separate socket for it.

**App → board, command:**

```json
{
  "type": "command",
  "device_uid": "3CDC75413DE8",
  "seq": 1,
  "request_id": "abcd1234",
  "payload": { "command": "set_wifi", "ssid": "...", "password": "..." }
}
```

Two commands exist today (`handleCommand()` in the firmware):
- `set_wifi` — payload `{ssid, password}`. Saves new WiFi creds to the
  board's NVS. Does **not** reboot on its own.
- `reboot` — no extra payload. Board acks then restarts.

Anything else gets `{"ok": false, "message": "unknown_command"}`.

**Board → app, ack (sent immediately on receiving any command):**

```json
{ "type": "ack", "device_uid": "3CDC75413DE8", "ack": 1, "request_id": "abcd1234" }
```

**Board → app, result (sent after processing):**

```json
{ "type": "result", "device_uid": "3CDC75413DE8", "request_id": "abcd1234", "ok": true, "message": "wifi credentials saved" }
```

A v1 Android app likely doesn't need to *send* commands at all (WiFi setup
is manual, §5.1) — but must not crash if it receives `ack`/`result` frames
it isn't expecting, since they arrive on the same socket as everything
else.

### 3.5 Distinguishing binary vs. JSON on the shared port-13250 socket

The reference gateway's own disambiguation rule (`main.py`): if the first
byte of a received UDP payload is `{` (0x7B), treat it as JSON control
traffic; otherwise treat it as a binary sensor/heartbeat packet (check the
`0xA55A` magic at offset 0 to confirm). Use the same rule.

---

## 4. Recommended architecture

- **Language/UI**: Kotlin + Jetpack Compose. No strong reason to use Java
  or XML layouts for a from-scratch app in 2026.
- **Networking**: plain `java.net.DatagramSocket` / `DatagramPacket` in a
  dedicated coroutine on `Dispatchers.IO`. No need for Netty or other
  heavyweight networking libraries — the protocol is simple enough that
  raw sockets are the right level of abstraction. Wrap receive loops in a
  `Flow` that the UI layer collects.
- **3D orientation view**: `Filament` (Google's real-time rendering
  engine, has first-class Android/Kotlin support) or `SceneView` (a
  higher-level wrapper around Filament made for exactly this kind of use
  case). Avoid rolling raw OpenGL ES by hand unless there's a specific
  reason to.
- **Charts**: `MPAndroidChart` (mature, well-documented, handles
  real-time scrolling line charts well) or Compose-native alternatives
  like `Vico` if a more modern API is preferred.
- **Local storage / CSV export**: Android's Storage Access Framework
  (`MediaStore` API for saving to a public Downloads-equivalent
  location) — mirror the existing CSV column layout exactly (`timestamp,
  seq, ax, ay, az, gx, gy, gz`) so recordings stay compatible with
  whatever analysis workflow already exists for files produced by the
  current dashboard.
- **Multi-board support**: model this from day one as a
  `Map<deviceUid, DeviceSession>`, even if the v1 UI only shows one board
  at a time — the reference dashboard already supports multiple
  simultaneous boards (`imu_viewer/app.py`'s per-UID state, `imu_viewer/templates/index.html`'s
  board-card overview strip) and matching that later is much easier if
  the data model supports it from the start.

### Suggested module layout

```
app/
  src/main/java/.../
    net/
      FindMeClient.kt       # UDP 22346: broadcast/offer/probe logic
      StreamReceiver.kt     # UDP 13250: binary packet parsing + control JSON
      PacketCodec.kt        # header pack/unpack, magic/version checks
    model/
      DeviceSession.kt      # per-board state: uid, name, rssi, last-seen, live?
      ImuSample.kt          # ax..gz + seq + timestamp
    ui/
      DeviceListScreen.kt   # the "boards overview" equivalent
      LiveViewScreen.kt     # 3D + stats, per selected board
      ChartsScreen.kt
      RecordingsScreen.kt
    recording/
      CsvRecorder.kt
```

---

## 5. Feature-by-feature plan, mapped to the existing dashboard

Match `imu_viewer/templates/index.html` and `imu_viewer/app.py` feature for
feature — that's the full v-final scope. Suggested build order (also see
§7 for phasing):

### 5.1 WiFi setup (manual — do not build the Windows auto-join equivalent)

Board ships / resets to broadcasting `DentalMotion-Setup-<uid>`, an open
WiFi hotspot, LED blinking blue. Flow:
1. User backgrounds the app (or the app detects "no boards found") and is
   shown a simple instruction screen: "Connect your phone's WiFi to
   `DentalMotion-Setup-XXXXXXXXXXXX`, then come back."
2. Once on that network, either open `http://192.168.4.1/` in a system
   browser/WebView (the board serves its own captive-portal HTML — no
   need to reimplement that page) — or, if a more integrated feel is
   wanted, POST directly to `http://192.168.4.1/save` with form fields
   `ssid` and `password` from inside the app (this is exactly what the
   board's `handlePortalSave()` expects — a normal `application/x-www-form-urlencoded`
   POST, no auth, no JSON). Either works; the direct-POST approach is a
   nicer UX if there's appetite for it, but the WebView-to-existing-page
   approach is less code.
3. Prompt the user to reconnect their phone to the normal WiFi network.
4. Resume discovery listening (§3.3) — the board will be broadcasting
   `findme_discover` on that network within a few seconds of reconnecting.

### 5.2 Discovery + attach

Implement §3.3 exactly. On receiving a `findme_discover` for a UID not
already attached, send the `findme_offer` back immediately. Track
per-device last-seen timestamp for the "live" vs. "offline" status the
reference UI shows.

### 5.3 Live data view

Parse binary packets per §3.2. Maintain a rolling window (the reference
dashboard uses 15 seconds / up to 1500 samples at 100 Hz) for the charts
and a "peak |g|" + shock counter (reference: shock = magnitude above
2.5g, see the dashboard's existing threshold if replicating exactly).

### 5.4 3D orientation

The reference dashboard derives pitch/roll/yaw from the raw accel/gyro in
JavaScript (`three.min.js` + the page's own orientation math) —
read `imu_viewer/templates/index.html`'s orientation-calculation JS
directly for the exact formula before reimplementing in Kotlin, rather
than re-deriving it from scratch, to keep displayed angles consistent
with the existing tool.

### 5.5 Recording

Start/stop, write CSV with the exact column layout in §4, save to a
user-visible location, list past recordings with size/date.

### 5.6 Multi-board overview

Mirror the "boards overview" cards feature (already built and working —
see `imu_viewer/templates/index.html`'s `.board-card` / `renderBoardCards`
for the exact UX: color-coded per-device label, green "LIVE" vs. muted
"offline" status, click to select). The color-assignment scheme there is
a deterministic hash of the UID into one of six named colors
(`imu_viewer/app.py`'s `_label_for`) — reuse the same hash so a given
physical board shows the same color whether viewed from the Android app
or the existing web dashboard.

---

## 6. Explicitly out of scope for v1

- Any Bluetooth transport (see §2, known broken on this hardware).
- The Windows-style automatic WiFi-network-switching provisioning flow
  (§2, item 5) — manual only.
- Background/always-on reception when the app isn't foregrounded (§2,
  item 3) — revisit only if there's a real need, as it requires a
  foreground service + battery-exemption UX that adds real complexity.
- Cloud sync / remote access from outside the local WiFi network — if
  that's ever wanted, the right tool is something like Tailscale
  (a VPN into the local network), not a rearchitecture of this app.

---

## 7. Suggested phased build order

1. **Protocol proof-of-concept** (no UI): a bare-bones Kotlin
   console/test-harness that binds both UDP sockets, sends/receives the
   FindMe handshake against a real board, and logs parsed IMU floats to
   Logcat. Don't write any UI until this works — it's where the
   MulticastLock gotcha (§2.2) and any protocol subtleties will surface.
2. **Minimal live-view UI**: device list + raw numeric accel/gyro values
   updating live, no 3D, no charts. Proves the full pipeline end-to-end
   in an actual app.
3. **3D orientation + charts** (§5.4, §5.3).
4. **Recording** (§5.5).
5. **Multi-board overview** (§5.6) + WiFi setup flow polish (§5.1).

Each phase should be validated against a real physical board before
moving to the next — this protocol has already produced several
hard-to-predict surprises on real hardware in the reference project
(the BLE crash, an OTA-partition issue causing a board to silently keep
running old firmware after a reflash, WiFi-scan timing quirks on
Windows) severe enough that "should work" was repeatedly wrong until
tested. Assume the same discipline applies here.

---

## 8. Reference material checklist

Before starting, pull the current version of these files from
`shashanksbharadwaj161/Dentalmotion-monitor` (this plan is a snapshot —
the repo is the living source of truth):

- `firmware/dentalmotion_board/dentalmotion_board.ino` — the entire wire
  protocol, verbatim.
- `gateway/dentalmotion_gateway/arduino_protocol.py` and `discovery.py` —
  the reference gateway's parsing/response logic, useful for
  cross-checking §3 against a second independent implementation.
- `imu_viewer/templates/index.html` — the exact 3D-orientation math,
  the board-card color/status UX, the chart windowing/thresholds.
- `imu_viewer/app.py` — per-device state shape, CSV column layout.
- `README.md` §6 — the human-facing WiFi setup instructions (useful for
  writing the app's own in-app instructions consistently).
