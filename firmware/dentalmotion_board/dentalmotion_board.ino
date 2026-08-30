// DentalMotion Monitor firmware — ESP32-S3-MINI-1 ("DentalMotion-ESP32S3 v1.0")
//
// Minimal, single-purpose sketch: IMU-only wrist motion monitor.
// Speaks the exact wire protocol expected by gateway/dentalmotion_gateway and
// imu_viewer/app.py (see arduino_protocol.py, discovery.py, udp_control.py,
// main.py::handle_udp_control, and _parse_packet in app.py). Fully local:
// no cloud dependency, no external branding.
//
// WiFi setup is a captive-portal SoftAP (connect a phone/laptop to the
// board's own hotspot, enter WiFi credentials in a browser page). An
// alternate BLE-provisioning approach was tried and reverted — it
// crash-looped on every boot into setup mode on this exact chip/toolchain
// (assert failed: block_trim_free heap_tlsf.c:371, during BT controller
// init) and was never reachable via any phone. This SoftAP method is the
// one actually verified working end-to-end on real hardware.
//
// Board:    ESP32-S3-MINI-1, 8MB flash, no PSRAM
// FQBN:     esp32:esp32:esp32s3:FlashSize=8M,PartitionScheme=default_8MB
// IMU:      BMI270 accel+gyro only, I2C SDA=45 SCL=42 @ 400kHz
// Library:  Arduino_BMI270_BMM150 (Library Manager), ArduinoJson (Library Manager)

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include <Wire.h>
#include <Arduino_BMI270_BMM150.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <strings.h>

// ---------------------------------------------------------------------------
// Protocol constants (must match gateway/dentalmotion_gateway/arduino_protocol.py
// and gateway/dentalmotion_gateway/discovery.py exactly)
// ---------------------------------------------------------------------------
static const uint16_t FINDME_PORT   = 22346;  // discovery.py default listen_discovery_port
static const uint16_t STREAM_PORT   = 13250;  // udp_control.py _DEVICE_CONTROL_PORT / listen_udp_port
static const uint16_t PACKET_MAGIC  = 0xA55A;
static const uint8_t  PACKET_VERSION = 3;
static const uint8_t  FLAG_IMU       = 0x01;
static const uint8_t  FLAG_HEARTBEAT = 0x80;
static const char*    PROTOCOL_TAG   = "DentalMotion/1";
static const char*    FIRMWARE_VERSION = "v1.0.0";
static const char*    HARDWARE_MODEL   = "DentalMotion-ESP32S3 v1.0";

static const uint32_t IMU_SAMPLE_INTERVAL_MS = 10;   // ~100Hz
static const uint32_t HEARTBEAT_INTERVAL_MS  = 3000;
static const uint32_t FINDME_INTERVAL_MS     = 3000;

// Status LED: single NeoPixel on GPIO11 (identified by physical probing —
// this board has no silkscreen/datasheet reference for it, just "SYS").
static const uint8_t  LED_PIN = 11;
static const uint8_t  BOOT_CYCLES_FOR_DISCOVERY = 5;    // power off/on this many times fast
static const uint32_t BOOT_COUNTER_RESET_MS     = 20000; // ...within this long a window each time

// Alternate, deterministic way to force setup mode: hold the BOOT button
// (GPIO0, same pin esptool uses for download mode) while powering on. Far
// more reliable than counting power cycles by hand — a quick switch flip may
// not fully discharge the board's decoupling cap, silently skipping a reset.
static const uint8_t  BOOT_BUTTON_PIN = 0;

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
Preferences prefs;
WebServer portalServer(80);
DNSServer dnsServer;
WiFiUDP findmeUdp;
WiFiUDP streamUdp;
Adafruit_NeoPixel statusLed(1, LED_PIN, NEO_GRB + NEO_KHZ800);

enum LedMode {
  LED_MODE_IMU_FAIL,     // solid-ish red blink: IMU init failed, halted
  LED_MODE_SETUP,        // blue blink: broadcasting the setup AP
  LED_MODE_CONNECTING,   // dim white blink: joining stored WiFi
  LED_MODE_WAITING,      // amber blink: on WiFi, waiting for a gateway
  LED_MODE_ATTACHED,     // solid green: streaming to a gateway
};
LedMode ledMode = LED_MODE_CONNECTING;
unsigned long normalOpStartMs = 0;
bool bootCounterCleared = true;

uint8_t macBytes[6];
char uidHex[13];           // 12 hex chars + NUL, uppercase
String apName;

bool setupMode = false;    // true while running the captive-portal SoftAP

bool attached = false;
IPAddress gatewayIp;
uint16_t gatewayPort = 0;
String currentGatewayId;

uint32_t txSeq = 0;
unsigned long lastImuSampleMs = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long lastFindmeMs = 0;

// ---------------------------------------------------------------------------
// Forward declarations (explicit, rather than relying on the Arduino IDE's
// ctags-based auto-prototype step, which chokes on some of these signatures)
// ---------------------------------------------------------------------------
void computeUid();
IPAddress broadcastAddressForCurrentSubnet();
void handlePortalRoot();
void handlePortalSave();
void handlePortalRedirect();
void startSetupAp();
void sendFindmeDiscover(const char* currentGatewayId);
void serviceFindme();
void writeHeader(uint8_t* out, uint8_t flags, uint16_t payloadLen);
void sendImuPacket(float ax, float ay, float az, float gx, float gy, float gz);
void sendHeartbeatPacket();
void sendControlFrame(JsonDocument& doc, IPAddress destIp, uint16_t destPort);
void handleCommand(const char* command, JsonVariantConst payload, const char* requestId,
                    IPAddress destIp, uint16_t destPort);
void serviceControl();
bool connectToStoredWifi();
void serviceLed();

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
void computeUid() {
  WiFi.macAddress(macBytes);
  static const char hexd[] = "0123456789ABCDEF";
  for (int i = 0; i < 6; i++) {
    uidHex[i * 2]     = hexd[(macBytes[i] >> 4) & 0xF];
    uidHex[i * 2 + 1] = hexd[macBytes[i] & 0xF];
  }
  uidHex[12] = '\0';
}

// ---------------------------------------------------------------------------
// Status LED (single NeoPixel, GPIO11). Non-blocking — driven by millis(),
// safe to call every loop() iteration from any mode.
// ---------------------------------------------------------------------------
void serviceLed() {
  unsigned long now = millis();
  bool blinkPhase = ((now / 400) % 2) == 0;
  uint32_t color = 0;
  switch (ledMode) {
    case LED_MODE_IMU_FAIL:
      color = blinkPhase ? statusLed.Color(60, 0, 0) : statusLed.Color(0, 0, 0);
      break;
    case LED_MODE_SETUP:
      color = blinkPhase ? statusLed.Color(0, 0, 60) : statusLed.Color(0, 0, 0);
      break;
    case LED_MODE_CONNECTING:
      color = blinkPhase ? statusLed.Color(25, 25, 25) : statusLed.Color(0, 0, 0);
      break;
    case LED_MODE_WAITING:
      color = blinkPhase ? statusLed.Color(50, 30, 0) : statusLed.Color(0, 0, 0);
      break;
    case LED_MODE_ATTACHED:
      color = statusLed.Color(0, 50, 0);
      break;
  }
  statusLed.setPixelColor(0, color);
  statusLed.show();
}

IPAddress broadcastAddressForCurrentSubnet() {
  IPAddress ip = WiFi.localIP();
  IPAddress mask = WiFi.subnetMask();
  IPAddress bcast;
  for (int i = 0; i < 4; i++) {
    bcast[i] = ip[i] | (~mask[i] & 0xFF);
  }
  return bcast;
}

// ---------------------------------------------------------------------------
// Captive-portal SoftAP (WiFi setup) — only used when no credentials stored,
// or the stored credentials failed to connect.
// ---------------------------------------------------------------------------
const char PORTAL_HTML_HEAD[] PROGMEM =
  "<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
  "<title>DentalMotion Setup</title><style>"
  "body{font-family:sans-serif;max-width:420px;margin:24px auto;padding:0 16px}"
  "h1{font-size:20px}label{display:block;margin-top:12px;font-size:14px}"
  "input,select{width:100%;padding:8px;font-size:16px;box-sizing:border-box;margin-top:4px}"
  "button{margin-top:18px;width:100%;padding:12px;font-size:16px;background:#2563eb;color:#fff;border:0;border-radius:6px}"
  "</style></head><body><h1>DentalMotion Monitor WiFi Setup</h1>"
  "<form method='POST' action='/save'>"
  "<label>Network</label><select name='ssid_select' onchange=\"document.getElementById('ssid').value=this.value\">";

const char PORTAL_HTML_TAIL[] PROGMEM =
  "</select>"
  "<label>SSID</label><input type='text' name='ssid' id='ssid' required>"
  "<label>Password</label><input type='password' name='password'>"
  "<button type='submit'>Save &amp; Connect</button>"
  "</form></body></html>";

void handlePortalRoot() {
  String html;
  html.reserve(2048);
  html += FPSTR(PORTAL_HTML_HEAD);
  html += "<option value=''>-- scan --</option>";
  int n = WiFi.scanComplete();
  if (n < 0) {
    WiFi.scanNetworks(true);
  } else {
    for (int i = 0; i < n; i++) {
      html += "<option value='" + WiFi.SSID(i) + "'>" + WiFi.SSID(i) + "</option>";
    }
  }
  html += FPSTR(PORTAL_HTML_TAIL);
  portalServer.send(200, "text/html", html);
}

void handlePortalSave() {
  String ssid = portalServer.arg("ssid");
  String password = portalServer.arg("password");
  if (ssid.length() == 0) {
    portalServer.send(400, "text/plain", "ssid required");
    return;
  }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", password);
  portalServer.send(200, "text/html",
    "<html><body><h3>Saved. Rebooting onto your network...</h3></body></html>");
  delay(500);
  ESP.restart();
}

void handlePortalRedirect() {
  portalServer.sendHeader("Location", "http://192.168.4.1/", true);
  portalServer.send(302, "text/plain", "");
}

void startSetupAp() {
  setupMode = true;
  ledMode = LED_MODE_SETUP;
  apName = String("DentalMotion-Setup-") + uidHex;

  WiFi.mode(WIFI_AP);
  IPAddress apIp(192, 168, 4, 1);
  IPAddress apMask(255, 255, 255, 0);
  WiFi.softAPConfig(apIp, apIp, apMask);
  WiFi.softAP(apName.c_str());
  WiFi.scanNetworks(true); // kick off async scan for the portal's network list

  dnsServer.start(53, "*", apIp);

  portalServer.on("/", HTTP_GET, handlePortalRoot);
  portalServer.on("/portal", HTTP_GET, handlePortalRoot);
  portalServer.on("/hotspot-detect.html", HTTP_GET, handlePortalRoot);
  portalServer.on("/save", HTTP_POST, handlePortalSave);
  portalServer.on("/generate_204", HTTP_GET, handlePortalRedirect);
  portalServer.on("/gen_204", HTTP_GET, handlePortalRedirect);
  portalServer.on("/connecttest.txt", HTTP_GET, handlePortalRedirect);
  portalServer.on("/ncsi.txt", HTTP_GET, handlePortalRedirect);
  portalServer.onNotFound(handlePortalRedirect);
  portalServer.begin();
}

// ---------------------------------------------------------------------------
// FindMe discovery (UDP broadcast, port 22346)
// ---------------------------------------------------------------------------
void sendFindmeDiscover(const char* currentGatewayId) {
  StaticJsonDocument<384> doc;
  doc["type"] = "findme_discover";
  doc["device_uid"] = uidHex;
  doc["device_name"] = String("DentalMotion Monitor-") + uidHex;
  doc["mode"] = "normal";
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["hardware_model"] = HARDWARE_MODEL;
  doc["wifi_rssi"] = WiFi.RSSI();
  doc["protocol"] = PROTOCOL_TAG;
  if (currentGatewayId != nullptr) {
    doc["current_gateway_id"] = currentGatewayId;
  }
  char buf[384];
  size_t len = serializeJson(doc, buf, sizeof(buf));

  findmeUdp.beginPacket(IPAddress(255, 255, 255, 255), FINDME_PORT);
  findmeUdp.write((const uint8_t*)buf, len);
  findmeUdp.endPacket();

  IPAddress subnetBcast = broadcastAddressForCurrentSubnet();
  if (subnetBcast != IPAddress(255, 255, 255, 255)) {
    findmeUdp.beginPacket(subnetBcast, FINDME_PORT);
    findmeUdp.write((const uint8_t*)buf, len);
    findmeUdp.endPacket();
  }
}

void serviceFindme() {
  if (!attached && millis() - lastFindmeMs >= FINDME_INTERVAL_MS) {
    lastFindmeMs = millis();
    sendFindmeDiscover(nullptr);
  }

  int packetSize = findmeUdp.parsePacket();
  if (packetSize <= 0) return;

  char buf[512];
  int len = findmeUdp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = '\0';

  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, buf) != DeserializationError::Ok) return;

  const char* type = doc["type"] | "";

  if (strcmp(type, "findme_offer") == 0) {
    const char* devUid = doc["device_uid"] | "";
    if (strcasecmp(devUid, uidHex) != 0) return;
    bool accept = doc["accept"] | false;
    if (!accept) return;
    if (attached) return; // already streaming to a gateway; don't hop uninvited
    uint16_t offeredPort = doc["udp_port"] | 0;
    if (offeredPort == 0) return;
    gatewayIp = findmeUdp.remoteIP();
    gatewayPort = offeredPort;
    currentGatewayId = String((const char*)(doc["gateway_id"] | ""));
    attached = true;
    ledMode = LED_MODE_ATTACHED;
  } else if (strcmp(type, "findme_probe") == 0) {
    sendFindmeDiscover(attached ? currentGatewayId.c_str() : nullptr);
  }
}

// ---------------------------------------------------------------------------
// Sensor/heartbeat UDP stream + UDP control channel (both on port 13250)
// ---------------------------------------------------------------------------
void writeHeader(uint8_t* out, uint8_t flags, uint16_t payloadLen) {
  out[0] = PACKET_MAGIC & 0xFF;
  out[1] = (PACKET_MAGIC >> 8) & 0xFF;
  out[2] = PACKET_VERSION;
  out[3] = flags;
  memcpy(out + 4, macBytes, 6);
  uint32_t seq = txSeq++;
  memcpy(out + 10, &seq, 4);
  uint32_t ts = millis();
  memcpy(out + 14, &ts, 4);
  out[18] = payloadLen & 0xFF;
  out[19] = (payloadLen >> 8) & 0xFF;
}

void sendImuPacket(float ax, float ay, float az, float gx, float gy, float gz) {
  if (!attached) return;
  uint8_t buf[20 + 28];
  writeHeader(buf, FLAG_IMU, 28);
  float payload[7] = { ax, ay, az, gx, gy, gz, 0.0f };
  memcpy(buf + 20, payload, sizeof(payload));

  streamUdp.beginPacket(gatewayIp, gatewayPort);
  streamUdp.write(buf, sizeof(buf));
  streamUdp.endPacket();
}

void sendHeartbeatPacket() {
  if (!attached) return;
  uint8_t buf[20];
  writeHeader(buf, FLAG_HEARTBEAT, 0);
  streamUdp.beginPacket(gatewayIp, gatewayPort);
  streamUdp.write(buf, sizeof(buf));
  streamUdp.endPacket();
}

void sendControlFrame(JsonDocument& doc, IPAddress destIp, uint16_t destPort) {
  char buf[384];
  size_t len = serializeJson(doc, buf, sizeof(buf));
  streamUdp.beginPacket(destIp, destPort);
  streamUdp.write((const uint8_t*)buf, len);
  streamUdp.endPacket();
}

void handleCommand(const char* command, JsonVariantConst payload, const char* requestId,
                    IPAddress destIp, uint16_t destPort) {
  StaticJsonDocument<256> result;
  result["type"] = "result";
  result["device_uid"] = uidHex;
  result["request_id"] = requestId;

  if (strcmp(command, "set_wifi") == 0) {
    const char* ssid = payload["ssid"] | "";
    const char* password = payload["password"] | "";
    if (strlen(ssid) == 0) {
      result["ok"] = false;
      result["message"] = "ssid_required";
    } else {
      prefs.putString("ssid", ssid);
      prefs.putString("pass", password);
      result["ok"] = true;
      result["message"] = "wifi credentials saved";
    }
  } else if (strcmp(command, "reboot") == 0) {
    result["ok"] = true;
    result["message"] = "rebooting";
    sendControlFrame(result, destIp, destPort);
    delay(200);
    ESP.restart();
    return;
  } else {
    result["ok"] = false;
    result["message"] = "unknown_command";
  }
  sendControlFrame(result, destIp, destPort);
}

void serviceControl() {
  int packetSize = streamUdp.parsePacket();
  if (packetSize <= 0) return;

  IPAddress senderIp = streamUdp.remoteIP();
  uint16_t senderPort = streamUdp.remotePort();

  char buf[512];
  int len = streamUdp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = '\0';
  if (buf[0] != '{') return; // binary sensor data doesn't loop back here

  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, buf) != DeserializationError::Ok) return;

  const char* type = doc["type"] | "";
  if (strcmp(type, "command") != 0) return;

  const char* devUid = doc["device_uid"] | "";
  if (strcasecmp(devUid, uidHex) != 0) return;

  uint32_t seq = doc["seq"] | 0;
  const char* requestId = doc["request_id"] | "";

  StaticJsonDocument<192> ack;
  ack["type"] = "ack";
  ack["device_uid"] = uidHex;
  ack["ack"] = seq;
  ack["request_id"] = requestId;
  sendControlFrame(ack, senderIp, senderPort);

  JsonVariantConst payload = doc["payload"];
  const char* command = payload["command"] | "";
  handleCommand(command, payload, requestId, senderIp, senderPort);
}

// ---------------------------------------------------------------------------
// WiFi connect (normal, non-setup boot)
// ---------------------------------------------------------------------------
bool connectToStoredWifi() {
  String ssid = prefs.getString("ssid", "");
  String password = prefs.getString("pass", "");
  if (ssid.length() == 0) return false;

  ledMode = LED_MODE_CONNECTING;
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), password.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    serviceLed();
    delay(50);
  }
  return WiFi.status() == WL_CONNECTED;
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);

  // Boot-cycle counting happens FIRST, before anything slow (IMU init in
  // particular can take a noticeable moment) — a fast power-switch flip
  // needs this saved to NVS within the first few milliseconds of boot, or a
  // quick-enough flip can cut power again before the increment ever
  // persists, silently dropping that cycle from the count.
  WiFi.mode(WIFI_STA);
  computeUid();
  prefs.begin("wifi", false);

  // Primary "force setup mode" trigger: hold the BOOT button while powering
  // on. GPIO0 — same pin esptool uses for download mode.
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
  bool bootHeldAtPowerOn = (digitalRead(BOOT_BUTTON_PIN) == LOW);

  // Secondary trigger, kept for boards/cases where holding BOOT isn't handy:
  // power the board off/on this many times, each within BOOT_COUNTER_RESET_MS
  // of the last.
  uint8_t bootCount = prefs.getUChar("bootcnt", 0) + 1;
  bool forceSetupMode = false;
  if (bootCount >= BOOT_CYCLES_FOR_DISCOVERY) {
    bootCount = 0;
    forceSetupMode = true;
  }
  prefs.putUChar("bootcnt", bootCount);

  statusLed.begin();
  statusLed.setBrightness(255);
  ledMode = LED_MODE_CONNECTING;
  serviceLed();

  Wire.begin(45, 42, 400000);
  if (!IMU.begin(BOSCH_ACCELEROMETER_ONLY)) {
    ledMode = LED_MODE_IMU_FAIL;
    while (true) {
      serviceLed();
      delay(50);
    }
  }
  IMU.setContinuousMode();

  if (bootHeldAtPowerOn || forceSetupMode || !connectToStoredWifi()) {
    startSetupAp();
    return; // stay dumb: setup mode only services the portal until reboot
  }

  ledMode = LED_MODE_WAITING;
  findmeUdp.begin(FINDME_PORT);
  streamUdp.begin(STREAM_PORT);
  normalOpStartMs = millis();
  bootCounterCleared = false;
}

void loop() {
  serviceLed();

  if (setupMode) {
    dnsServer.processNextRequest();
    portalServer.handleClient();
    return;
  }

  if (!bootCounterCleared && millis() - normalOpStartMs >= BOOT_COUNTER_RESET_MS) {
    prefs.putUChar("bootcnt", 0);
    bootCounterCleared = true;
  }

  if (WiFi.status() != WL_CONNECTED) {
    // Transient drop — the ESP32 WiFi stack auto-reconnects with the stored
    // credentials. Just skip this iteration's network work instead of
    // bouncing back into setup mode.
    if (attached) ledMode = LED_MODE_WAITING;
    attached = false;
    delay(50);
    return;
  }

  serviceFindme();
  serviceControl();

  unsigned long now = millis();

  if (now - lastImuSampleMs >= IMU_SAMPLE_INTERVAL_MS) {
    lastImuSampleMs = now;
    float ax, ay, az, gx, gy, gz;
    if (IMU.accelerationAvailable() && IMU.gyroscopeAvailable()) {
      IMU.readAcceleration(ax, ay, az);
      IMU.readGyroscope(gx, gy, gz);
      sendImuPacket(ax, ay, az, gx, gy, gz);
    }
  }

  if (now - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    sendHeartbeatPacket();
  }
}
