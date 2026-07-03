# DentalMotion Monitor — Setup & User Guide

**A real-time hand-motion monitoring system for dental brushing research.**
Records wrist and hand movements during tooth brushing, flossing, and other oral-care tasks.
Displays live 3D orientation, acceleration graphs, and shock/impact detection.
Saves all sessions as CSV files you can open directly in Excel for analysis.

> **This guide is written for everyone — no technical knowledge required.**
> If you can install an app and connect to WiFi, you can run this system.

---

## What Is This System?

A researcher or patient wears the sensor glove on their hand. As they brush their teeth (or perform
other oral-care motions), the glove wirelessly sends motion data to this laptop in real time.
The dashboard shows the movement live and records it for later analysis.

```
[Sensor Glove]  ──WiFi──►  [This Laptop]  ──►  [Browser Dashboard]
  on patient's                runs in the          shows 3D motion,
    hand                      background            graphs, records
```

---

## What You Need

| Item | Details |
|------|---------|
| **Sensor Glove** | New Horizons OS board (pre-assembled, worn on the hand) |
| **Windows Laptop / PC** | Windows 10 or Windows 11 |
| **WiFi Network** | The laptop and the glove must be on the **same WiFi network** |
| **Browser** | Any browser (Chrome, Edge, Firefox) |

> The glove connects to WiFi automatically when switched on.
> It was pre-configured for your clinic/lab network.
> If the WiFi ever changes, see **Section 6 — Changing the WiFi**.

---

## One-Time Setup (Do This Once Only)

### Step 1 — Install Python

Python is a free program that runs the monitoring software.

1. Open your browser and go to: **https://www.python.org/downloads/**
2. Click the large **"Download Python"** button
3. Run the downloaded installer file
4. **⚠️ IMPORTANT:** On the first installer screen, tick the checkbox that says **"Add Python to PATH"** before clicking anything else

5. Click **Install Now** and wait (takes about 2 minutes)
6. Click **Close** when done

**To verify it worked:**
- Press the `Windows` key + `R` on your keyboard
- Type `cmd` and press Enter
- In the black window, type `python --version` and press Enter
- You should see something like `Python 3.13.2` — if so, you are ready

---

### Step 2 — Install the Required Components

1. Open the `dentalmotion-monitor` folder (the folder containing this README)
2. Click once in the **address bar** at the top of the folder window (it shows the folder path)
3. Type `cmd` and press Enter — a black window opens inside this folder
4. Copy and paste the line below into that black window, then press Enter:

```
pip install -r gateway\requirements.txt -r imu_viewer\requirements.txt
```

5. Wait for it to finish. You will see text scrolling — this is normal. It may take 1–3 minutes.
6. When you see `Successfully installed ...` it is complete.

> ✅ You only ever need to do Steps 1 and 2 once.
> From now on, just use `START.bat` every session.

---

## Daily Use — Recording a Session

### Step 3 — Put on the Glove and Power It On

1. Have the patient or researcher put on the sensor glove
2. Switch the glove on using the power switch on the board
3. The LED will blink while it connects to WiFi — wait until it stays solid (about 10–15 seconds)

---

### Step 4 — Start the Dashboard

1. Open the `dentalmotion-monitor` folder
2. **Double-click `START.bat`**
3. Two black windows will open — **do not close them**, they run in the background
4. Your browser will automatically open the dashboard

If the browser does not open automatically, open it and go to: **http://127.0.0.1:8765**

---

### Step 5 — Connect the Glove

When the dashboard opens, it may show "no data" for a moment.

1. Click the **Network** tab (fourth tab at the top)
2. Click **Rediscover Board**
3. Wait about 5–10 seconds
4. The status will change to **Stream: live** and the graphs will begin moving

---

### Step 6 — Record the Session

1. Click the **Recordings** tab (third tab)
2. Click **▶ Start Recording**
3. Ask the patient to begin brushing / performing the task
4. When the task is complete, click **■ Stop Recording**

The file is automatically saved to your Desktop in a folder called **DentalMotion\_Recordings**.

**File naming:** `imu_[device-id]_[date]_[time].csv`
Example: `imu_3CDC75413DC8_20260703_142500.csv`

**Opening in Excel:** Just double-click the file — Excel opens it automatically.
Each row = one sensor reading (~100 readings per second).
Columns: `timestamp`, `seq`, `ax`, `ay`, `az`, `gx`, `gy`, `gz`

---

### Step 7 — Stop the System

When all sessions for the day are done:

1. Click **■ Stop Recording** if a recording is still active
2. **Double-click `STOP.bat`** in the `dentalmotion-monitor` folder
3. Both black windows will close
4. Switch off the glove

---

## Understanding the Dashboard

### Tab 1 — 3D View

Shows the glove orientation as a rotating 3D model in real time.

| Element | What it means |
|---------|--------------|
| **Packets/s** | Data rate from the glove — should be 56–100 during use |
| **Seq Drops** | Missed packets — a few is normal; many means WiFi interference |
| **Pitch / Roll / Yaw** | Three rotation angles of the hand in degrees |
| **Peak \|g\| / Shocks** | Highest force measured and count of sudden movements above 2.5g |
| **Gold arrow** on model | Points in the direction of gravity — useful to see wrist tilt |
| **Pause** button | Freezes the 3D display (recording continues normally) |
| **⛶ Fullscreen** button | Expands the 3D model to fill the screen |

---

### Tab 2 — Charts

Live scrolling graphs showing the last 15 seconds of motion data:

- **Acceleration (g)** — Force on the X, Y, Z axes (range ±8g). Shows brushing strokes clearly.
- **Gyroscope (°/s)** — Rotation speed. Shows wrist rotation and flicking motions.
- **Magnitude \|g\|** — Overall movement intensity. The dashed line at 1g = resting baseline. Peaks show brushing strokes.

---

### Tab 3 — Recordings

Start and stop session recordings. Lists all previously recorded files with date, time, and file size.

---

### Tab 4 — Network

Manage the glove's WiFi connection. Use this tab when:
- The glove shows "no data" after powering on → click **Rediscover Board**
- The clinic WiFi name or password has changed → use **Change WiFi Credentials**

---

## Section 6 — Changing the WiFi Network

Use this when the clinic or hospital WiFi changes (new network name, new password, moved to a different building).

### Method A — Via the Dashboard (Use this first)

*Works when the glove is already connected and showing data.*

1. Open the **Network** tab
2. Under **Change WiFi Credentials**, enter the new network name in the **Network Name (SSID)** field
3. Enter the password (click 👁 to see what you are typing)
4. Click **Apply & Reboot Board**
5. A countdown timer will appear — the glove is saving the credentials and restarting
6. After the countdown, connect this laptop to the new WiFi network
7. Click **Rediscover Board** — the glove should reconnect within 10 seconds

---

### Method B — Hardware Recovery

*Use this when the glove cannot connect to any network (e.g., moved to a completely new location).*

You will need: **any phone, tablet, or laptop with WiFi**

1. **Power off** the glove completely
2. **Hold the BOOT button** — a small button on the sensor board labeled BOOT
3. While holding BOOT, **power the glove back on**
4. Keep holding BOOT for **3 seconds** after it powers on, then release
5. The glove creates a temporary WiFi network named **`NHOS-Setup-3CDC75`** (no password)
6. On your phone or another laptop, **connect to `NHOS-Setup-3CDC75`**
7. Open a browser and go to: **http://192.168.4.1**
8. A setup page appears — type the new WiFi name and password, then click Save
9. The glove restarts and joins the new network
10. Switch your phone back to the regular WiFi
11. On this PC, go to the **Network** tab → click **Rediscover Board**

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| Dashboard shows "no data" or flat graphs | Check glove LED is solid (not blinking). Go to **Network** tab → **Rediscover Board** |
| "Board not visible — wait a few seconds and try again" | Glove just connected. Wait 10 s and click **Rediscover** again |
| Status says "gateway offline" | Close both black windows and double-click `START.bat` again |
| Page shows an error or is blank | Wait 10 seconds and refresh the browser. Check the "IMU Viewer" black window is open |
| `python` is not recognized | Python was installed without "Add to PATH". Reinstall Python (Step 1) |
| Packets/s is 0 but status shows "live" | Click **Rediscover Board** in the Network tab |
| Signal strength (RSSI) below −75 dBm | Glove is too far from the WiFi router. Move closer or ask IT about coverage |
| Recorded file does not appear in Excel | Look on your Desktop in the folder called `DentalMotion_Recordings` |

---

## File Locations

| What | Location |
|------|---------|
| Recorded CSV files | `Desktop\DentalMotion_Recordings\` |
| Dashboard (while running) | http://127.0.0.1:8765 |
| Gateway config | `dentalmotion-monitor\gateway\config.json` |

---

## For IT Staff — System Architecture

```
[Sensor Glove]
  UDP port 13250 ──► [Gateway  :5052]  ──mirror──►  [IMU Viewer :8765]
  UDP port 22346 ──► (FindMe discovery)
                           │
                    WebSocket upstream:
              wss://isensing-s1.u-aizu.ac.jp
```

- **Gateway** listens: UDP 13250 (sensor data), UDP 22346 (FindMe), TCP 5052 (REST API)
- **IMU Viewer** listens: UDP 13253 (mirrored data), TCP 8765 (web dashboard)
- Both processes must run simultaneously
- WiFi credentials are changed via UDP command relay: `POST /api/devices/<uid>/cmd` → `{"command":"set_wifi", "ssid":"...", "password":"..."}`
- Board reboots automatically 1 second after credentials are applied
- Default device UID: `3CDC75413DC8` — override with env var `NHOS_DEVICE_UID`
- Recordings saved to: `%USERPROFILE%\Desktop\DentalMotion_Recordings\`

Sensor spec: BMI270 IMU — accelerometer ±8g / 100 Hz, gyroscope ±2000 °/s / 100 Hz

---

*DentalMotion Monitor — Built on New Horizons OS v0.10.1 — VD-CTL/R v1.0.F 2026.4*
