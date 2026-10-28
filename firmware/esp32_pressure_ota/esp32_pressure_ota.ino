#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <SPI.h>
#include <SD.h>
#include <Ethernet.h>
#include <Update.h>

// -----------------------------
// Existing hardware assignments
// -----------------------------
static const int SD_SCK  = 18;
static const int SD_MISO = 19;
static const int SD_MOSI = 23;
static const int SD_CS   = 5;

static const int ETH_SCK  = 25;
static const int ETH_MISO = 26;
static const int ETH_MOSI = 27;
static const int ETH_CS   = 32;
static const int ETH_RST  = 33;

static const char *AP_SSID = "PressureMonitor";
static const char *AP_PASS = "12345678";
static const uint16_t DEFAULT_EDGE_PORT = 9100;
static const uint32_t SAMPLE_PERIOD_US = 5000; // 200 Hz
static const size_t EDGE_BATCH_SAMPLES = 20;   // 100 ms batches
static const char *LOG_PATH = "/pressure_log.csv";
static const uint32_t OTA_IDLE_TIMEOUT_MS = 10000; // abort OTA if no data for 10 s

SPIClass sdSPI(HSPI);
Adafruit_ADS1115 ads;
WebServer server(80);
Preferences prefs;
EthernetClient edgeClient;

struct EngineeringConfig {
  float zeroVoltage = 0.03f;
  float slopeBarPerVolt = 14.63f;
  IPAddress deviceIp = IPAddress(192, 168, 1, 50);
  IPAddress gateway = IPAddress(192, 168, 1, 1);
  IPAddress subnet = IPAddress(255, 255, 255, 0);
  IPAddress dns = IPAddress(192, 168, 1, 1);
  IPAddress receiverIp = IPAddress(192, 168, 1, 10);
  uint16_t receiverPort = DEFAULT_EDGE_PORT;
};

EngineeringConfig cfg;
volatile float currentVoltage = 0.0f;
volatile float currentPressureBar = 0.0f;
volatile uint32_t totalSamples = 0;
volatile uint32_t droppedEdgeBatches = 0;
volatile bool recording = false;
bool sdReady = false;
bool adsReady = false;

File logFile;
String sdBuffer;
uint32_t lastSdFlushMs = 0;
uint32_t nextSampleUs = 0;
uint32_t edgeSequence = 0;
uint32_t lastEdgeConnectAttemptMs = 0;
uint32_t lastEdgeAckMs = 0;
String bootId;

int32_t batchPressureMbar[EDGE_BATCH_SAMPLES];
int32_t batchVoltageUv[EDGE_BATCH_SAMPLES];
uint32_t batchFirstSampleUs = 0;
size_t batchCount = 0;

// -----------------------------
// Utility / persistence
// -----------------------------
String ipToString(const IPAddress &ip) {
  return String(ip[0]) + "." + String(ip[1]) + "." + String(ip[2]) + "." + String(ip[3]);
}

bool parseIp(const String &text, IPAddress &out) {
  int a, b, c, d;
  if (sscanf(text.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) != 4) return false;
  if (a < 0 || a > 255 || b < 0 || b > 255 || c < 0 || c > 255 || d < 0 || d > 255) return false;
  out = IPAddress(a, b, c, d);
  return true;
}

void loadConfig() {
  prefs.begin("pressure-node", true);
  cfg.zeroVoltage = prefs.getFloat("zero_v", 0.03f);
  cfg.slopeBarPerVolt = prefs.getFloat("slope", 14.63f);
  String deviceIp = prefs.getString("eth_ip", "192.168.1.50");
  String gateway = prefs.getString("eth_gw", "192.168.1.1");
  String subnet = prefs.getString("eth_mask", "255.255.255.0");
  String dns = prefs.getString("eth_dns", "192.168.1.1");
  String receiver = prefs.getString("edge_ip", "192.168.1.10");
  cfg.receiverPort = prefs.getUShort("edge_port", DEFAULT_EDGE_PORT);
  prefs.end();
  parseIp(deviceIp, cfg.deviceIp);
  parseIp(gateway, cfg.gateway);
  parseIp(subnet, cfg.subnet);
  parseIp(dns, cfg.dns);
  parseIp(receiver, cfg.receiverIp);
}

void saveConfig() {
  prefs.begin("pressure-node", false);
  prefs.putFloat("zero_v", cfg.zeroVoltage);
  prefs.putFloat("slope", cfg.slopeBarPerVolt);
  prefs.putString("eth_ip", ipToString(cfg.deviceIp));
  prefs.putString("eth_gw", ipToString(cfg.gateway));
  prefs.putString("eth_mask", ipToString(cfg.subnet));
  prefs.putString("eth_dns", ipToString(cfg.dns));
  prefs.putString("edge_ip", ipToString(cfg.receiverIp));
  prefs.putUShort("edge_port", cfg.receiverPort);
  prefs.end();
}

// -----------------------------
// CRC32 / SMTCS-EDGE/1 framing
// -----------------------------
uint32_t crc32Bytes(const uint8_t *data, size_t len) {
  uint32_t crc = 0xFFFFFFFF;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit) {
      uint32_t mask = -(crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320U & mask);
    }
  }
  return ~crc;
}

String crcHex(const String &canonical) {
  uint32_t crc = crc32Bytes((const uint8_t *)canonical.c_str(), canonical.length());
  char buf[9];
  snprintf(buf, sizeof(buf), "%08lx", (unsigned long)crc);
  return String(buf);
}

String frameFromCanonical(const String &canonical) {
  String out = canonical;
  if (out.endsWith("}")) out.remove(out.length() - 1);
  out += ",\"crc32\":\"" + crcHex(canonical) + "\"}\n";
  return out;
}

String makeHelloFrame() {
  // Keys intentionally match Python json.dumps(sort_keys=True,separators=(",",":"))
  String canonical = "{\"boot_id\":\"" + bootId +
    "\",\"channels\":[\"pressure_mbar\",\"voltage_uv\"],\"device_id\":\"PT-01\",\"firmware\":\"esp32-pressure-edge/1+ota+hw+ign\",\"protocol\":\"SMTCS-EDGE/1\",\"type\":\"HELLO\"}";
  return frameFromCanonical(canonical);
}

String makeBatchFrame() {
  String channels = "{\"pressure_mbar\":[";
  for (size_t i = 0; i < batchCount; ++i) {
    if (i) channels += ',';
    channels += String(batchPressureMbar[i]);
  }
  channels += "],\"voltage_uv\":[";
  for (size_t i = 0; i < batchCount; ++i) {
    if (i) channels += ',';
    channels += String(batchVoltageUv[i]);
  }
  channels += "]}";

  String canonical = "{\"boot_id\":\"" + bootId +
    "\",\"channels\":" + channels +
    ",\"device_id\":\"PT-01\",\"first_sample_us\":" + String(batchFirstSampleUs) +
    ",\"protocol\":\"SMTCS-EDGE/1\",\"sample_count\":" + String(batchCount) +
    ",\"sample_period_us\":" + String(SAMPLE_PERIOD_US) +
    ",\"sequence\":" + String(edgeSequence) +
    ",\"type\":\"BATCH\"}";
  return frameFromCanonical(canonical);
}

// -----------------------------
// Firmware update over Ethernet (OTA)
// -----------------------------
// Uses the same TCP link as the telemetry. SFT STELLAR sends one line
//   {"device_id":..,"md5":"<32 hex>","protocol":"SMTCS-EDGE/1","size":<bytes>,"type":"OTA_BEGIN","crc32":..}
// the ESP32 answers OTA_READY, then SFT STELLAR sends exactly <size> raw bytes
// of the compiled .bin. The ESP32 checks the MD5, answers OTA_DONE and restarts.
// On any error it answers OTA_FAIL and keeps running the current firmware.

void stopRecording();   // defined below (SD logging)

bool frameCrcValid(const String &line) {
  int idx = line.lastIndexOf(",\"crc32\":\"");
  if (idx < 0 || line.length() < (unsigned)(idx + 18)) return false;
  String canonical = line.substring(0, idx) + "}";
  String supplied = line.substring(idx + 10, idx + 18);
  supplied.toLowerCase();
  return supplied == crcHex(canonical);
}

long frameIntField(const String &line, const char *key) {
  String token = String("\"") + key + "\":";
  int p = line.indexOf(token);
  if (p < 0) return -1;
  return line.substring(p + token.length()).toInt();
}

String frameStringField(const String &line, const char *key) {
  String token = String("\"") + key + "\":\"";
  int p = line.indexOf(token);
  if (p < 0) return "";
  p += token.length();
  return line.substring(p, line.indexOf('"', p));
}

// ============================================================================
//  IGNITION / RELAY MODULE
// ----------------------------------------------------------------------------
//  Receives IGN_ARM / IGN_FIRE / IGN_SAFE from the test-stand console over the
//  same Ethernet TCP link as the pressure data, and drives two outputs:
//
//    IGN_ARM_PIN   HIGH while ARMED  -> arming relay (series with the igniter)
//    IGN_FIRE_PIN  HIGH for pulse_ms -> fire relay / MOSFET
//
//  Wire the igniter so current flows ONLY when BOTH relays are closed.
//
//  Safety rules enforced here, independent of the PC:
//    * Outputs are driven LOW as the very first thing at boot.
//    * ARMED expires if no IGN_ARM keep-alive arrives for IGN_DEADMAN_MS.
//    * FIRE is accepted only while ARMED; pulse is capped at IGN_MAX_PULSE_MS.
//    * Frames with a bad CRC32 or a different target are ignored.
//    * Ethernet link to the console lost -> SAFE immediately.
//    * After a fire pulse the node returns to SAFE (must be re-armed).
//    * Relay test (IGN_TEST): from SAFE only, one relay at a time, max 1000 ms.
//
//  !! CHECK IGN_ARM_PIN / IGN_FIRE_PIN AGAINST YOUR WIRING BEFORE UPLOADING !!
//  !! First test with an LED or a lamp instead of an igniter.              !!
// ============================================================================

static const int IGN_FIRE_PIN = 15;    // relay (LED) input wired to GPIO 15
static const int IGN_ARM_PIN  = -1;    // no arming relay
static const bool IGN_ACTIVE_HIGH = true;
static const uint32_t IGN_DEADMAN_MS = 1500;
static const uint32_t IGN_MAX_PULSE_MS = 3000;
static const uint32_t IGN_STATUS_PERIOD_MS = 500;
static const uint32_t IGN_MAX_TEST_MS = 1000;
static const char *IGN_DEVICE_ID = "PT-01";

enum IgnState { IGN_SAFE_STATE, IGN_ARMED_STATE, IGN_FIRING_STATE, IGN_TEST_STATE };
IgnState ignState = IGN_SAFE_STATE;
uint32_t ignLastArmMs = 0;
uint32_t ignFireStartMs = 0;
uint32_t ignPulseMs = 0;
uint32_t ignFireCount = 0;
uint32_t ignLastStatusMs = 0;
int ignTestPin = -1;

void ignWrite(int pin, bool on) {
  if (pin < 0) return;
  digitalWrite(pin, (on == IGN_ACTIVE_HIGH) ? HIGH : LOW);
}

// Call as the FIRST line of setup().
void ignitionSetup() {
  if (IGN_FIRE_PIN >= 0) { pinMode(IGN_FIRE_PIN, OUTPUT); ignWrite(IGN_FIRE_PIN, false); }
  if (IGN_ARM_PIN >= 0)  { pinMode(IGN_ARM_PIN, OUTPUT);  ignWrite(IGN_ARM_PIN, false); }
  ignState = IGN_SAFE_STATE;
}

const char *ignStateName() {
  switch (ignState) {
    case IGN_ARMED_STATE:  return "ARMED";
    case IGN_FIRING_STATE: return "FIRING";
    case IGN_TEST_STATE:   return "TEST";
    default:               return "SAFE";
  }
}

void ignSendStatus() {
  if (!edgeClient.connected()) return;
  // keys in sorted order so the PC can verify the CRC
  String canonical = String("{\"ads_ready\":") + (adsReady ? "true" : "false") +
    ",\"boot_id\":\"" + bootId +
    "\",\"device_id\":\"" + String(IGN_DEVICE_ID) +
    "\",\"fire_count\":" + String(ignFireCount) +
    ",\"protocol\":\"SMTCS-EDGE/1\",\"sd_ready\":" + (sdReady ? "true" : "false") +
    ",\"state\":\"" + String(ignStateName()) +
    "\",\"type\":\"IGN_STATUS\"}";
  String frame = frameFromCanonical(canonical);
  edgeClient.write((const uint8_t *)frame.c_str(), frame.length());
  ignLastStatusMs = millis();
}

void ignGoSafe(const char *why) {
  ignWrite(IGN_FIRE_PIN, false);
  ignWrite(IGN_ARM_PIN, false);
  if (ignState != IGN_SAFE_STATE) {
    Serial.print("[IGN] SAFE: "); Serial.println(why);
    ignState = IGN_SAFE_STATE;
    ignSendStatus();
  }
}

long ignIntField(const String &line, const char *key, long fallback) {
  String token = String("\"") + key + "\":";
  int p = line.indexOf(token);
  if (p < 0) return fallback;
  return line.substring(p + token.length()).toInt();
}

// Called for every line received from the console.
void ignitionHandleLine(const String &line) {
  if (line.indexOf("\"type\":\"IGN_") < 0) return;
  if (!frameCrcValid(line)) { Serial.println("[IGN] frame rejected: CRC"); return; }
  if (line.indexOf(String("\"target\":\"") + IGN_DEVICE_ID + "\"") < 0) return;

  if (line.indexOf("\"type\":\"IGN_TEST\"") >= 0) {
    bool fireRelay = line.indexOf("\"relay\":\"fire\"") >= 0;
    bool armRelay = line.indexOf("\"relay\":\"arm\"") >= 0;
    int pin = fireRelay ? IGN_FIRE_PIN : (armRelay ? IGN_ARM_PIN : -1);
    if (ignState != IGN_SAFE_STATE || pin < 0) {
      Serial.println("[IGN] relay test refused");
      ignSendStatus();
      return;
    }
    long pulse = ignIntField(line, "pulse_ms", 300);
    if (pulse < 50) pulse = 50;
    if (pulse > (long)IGN_MAX_TEST_MS) pulse = IGN_MAX_TEST_MS;
    ignTestPin = pin;
    ignPulseMs = (uint32_t)pulse;
    ignFireStartMs = millis();
    ignState = IGN_TEST_STATE;
    ignWrite(pin, true);   // only this one relay
    Serial.print("[IGN] relay test "); Serial.println(fireRelay ? "FIRE" : "ARM");
    ignSendStatus();
  } else if (line.indexOf("\"type\":\"IGN_SAFE\"") >= 0) {
    ignGoSafe("IGN_SAFE from console");
    ignSendStatus();
  } else if (line.indexOf("\"type\":\"IGN_ARM\"") >= 0) {
    if (ignState == IGN_TEST_STATE) return;
    ignLastArmMs = millis();
    if (ignState == IGN_SAFE_STATE) {
      ignState = IGN_ARMED_STATE;
      ignWrite(IGN_ARM_PIN, true);
      Serial.println("[IGN] ARMED");
      ignSendStatus();
    }
  } else if (line.indexOf("\"type\":\"IGN_FIRE\"") >= 0) {
    if (ignState != IGN_ARMED_STATE || millis() - ignLastArmMs > IGN_DEADMAN_MS) {
      Serial.println("[IGN] FIRE refused: not armed");
      ignSendStatus();
      return;
    }
    long pulse = ignIntField(line, "pulse_ms", 1000);
    if (pulse < 50) pulse = 50;
    if (pulse > (long)IGN_MAX_PULSE_MS) pulse = IGN_MAX_PULSE_MS;
    ignPulseMs = (uint32_t)pulse;
    ignFireStartMs = millis();
    ignState = IGN_FIRING_STATE;
    ignFireCount++;
    ignWrite(IGN_FIRE_PIN, true);
    Serial.print("[IGN] FIRE "); Serial.print(ignPulseMs); Serial.println(" ms");
    ignSendStatus();
  }
}

// Called every loop().
void ignitionService() {
  uint32_t now = millis();
  if (ignState != IGN_SAFE_STATE && !edgeClient.connected()) {
    ignGoSafe("console link lost");
    return;
  }
  if (ignState == IGN_ARMED_STATE && now - ignLastArmMs > IGN_DEADMAN_MS) {
    ignGoSafe("dead-man: no ARM keep-alive");
  } else if (ignState == IGN_TEST_STATE && now - ignFireStartMs >= ignPulseMs) {
    ignGoSafe("relay test complete");
  } else if (ignState == IGN_FIRING_STATE && now - ignFireStartMs >= ignPulseMs) {
    ignWrite(IGN_FIRE_PIN, false);
    ignGoSafe("pulse complete");
  }
  if (now - ignLastStatusMs >= IGN_STATUS_PERIOD_MS) ignSendStatus();
}

void sendOtaFrame(const char *type, const String &message = "", long received = -1) {
  // keys in sorted order so SFT STELLAR can check the CRC
  String canonical = "{\"boot_id\":\"" + bootId + "\",\"device_id\":\"PT-01\"";
  if (message.length()) canonical += ",\"message\":\"" + message + "\"";
  canonical += ",\"protocol\":\"SMTCS-EDGE/1\"";
  if (received >= 0) canonical += ",\"received\":" + String(received);
  canonical += ",\"type\":\"" + String(type) + "\"}";
  String frame = frameFromCanonical(canonical);
  edgeClient.write((const uint8_t *)frame.c_str(), frame.length());
}

void handleOtaBegin(const String &line) {
  if (!frameCrcValid(line)) { sendOtaFrame("OTA_FAIL", "bad CRC"); return; }
  long size = frameIntField(line, "size");
  String md5 = frameStringField(line, "md5");
  if (size <= 0) { sendOtaFrame("OTA_FAIL", "bad size"); return; }
  if (recording) stopRecording();
  ignGoSafe("firmware update");
  if (!Update.begin((size_t)size)) { sendOtaFrame("OTA_FAIL", Update.errorString()); return; }
  if (md5.length() == 32) Update.setMD5(md5.c_str());
  Serial.printf("[OTA] receiving %ld bytes\n", size);
  sendOtaFrame("OTA_READY");

  static uint8_t buf[1460];
  long got = 0, lastProgress = 0;
  uint32_t lastDataMs = millis();
  while (got < size) {
    int avail = edgeClient.available();
    if (avail > 0) {
      size_t want = (size_t)min((long)sizeof(buf), size - got);
      if ((size_t)avail < want) want = (size_t)avail;
      int n = edgeClient.read(buf, want);
      if (n > 0) {
        if (Update.write(buf, (size_t)n) != (size_t)n) {
          sendOtaFrame("OTA_FAIL", Update.errorString());
          Update.abort();
          return;
        }
        got += n;
        lastDataMs = millis();
        if (got - lastProgress >= 65536) { sendOtaFrame("OTA_PROGRESS", "", got); lastProgress = got; }
      }
    } else {
      if (!edgeClient.connected() || millis() - lastDataMs > OTA_IDLE_TIMEOUT_MS) {
        Serial.println("[OTA] aborted: link lost or timeout");
        Update.abort();
        return;
      }
      delay(1);
    }
  }
  if (!Update.end(true)) { sendOtaFrame("OTA_FAIL", Update.errorString()); return; }
  Serial.println("[OTA] done, restarting");
  sendOtaFrame("OTA_DONE", "", got);
  edgeClient.flush();
  delay(500);
  ESP.restart();
}

// -----------------------------
// Hardware scan (what is physically connected)
// -----------------------------
// On HW_SCAN from SFT STELLAR the ESP32 answers HW_REPORT with:
//   used : the pins this firmware uses and what for
//   i2c  : every address that answers on the I2C bus (SDA 21 / SCL 22)
//   pins : the free pins read as INPUTS only (never driven), each:
//          "open"  = follows the internal pull-up and pull-down -> nothing connected
//          "low" / "high" = something holds the pin -> a device or wire is connected
//          "analog" (34/35/36/39, input-only, no pulls) with the voltage in mV
struct PinUse { int pin; const char *use; };
static const PinUse USED_PINS[] = {
  {5, "SD CS"}, {18, "SD SCK"}, {19, "SD MISO"}, {23, "SD MOSI"},
  {21, "I2C SDA"}, {22, "I2C SCL"},
  {25, "ETH SCK"}, {26, "ETH MISO"}, {27, "ETH MOSI"}, {32, "ETH CS"}, {33, "ETH RST"},
  {1, "USB serial TX"}, {3, "USB serial RX"},
  {15, "Relay (FIRE)"},
};
static const uint8_t I2C_USED[] = {0x48};                           // ADS1115
// When you add a sensor in code, add its pin(s) to USED_PINS / its address to I2C_USED
// so SFT STELLAR shows it as "in code" instead of "no code".
static const int PROBE_PINS[] = {2, 4, 12, 13, 14, 16, 17};   // free, have internal pulls
static const int ANALOG_PINS[] = {34, 35, 36, 39};                  // free, input-only

String hwReportCanonical() {
  // keys sorted alphabetically at every level (SFT STELLAR checks the CRC)
  String i2c = "[";
  bool first = true;
  for (uint8_t addr = 0x08; addr <= 0x77; ++addr) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      if (!first) i2c += ',';
      i2c += String(addr);
      first = false;
    }
  }
  i2c += "]";

  String pins = "[";
  first = true;
  for (int pin : PROBE_PINS) {
    if ((pin == 16 || pin == 17) && psramFound()) continue;   // WROVER: 16/17 belong to PSRAM
    pinMode(pin, INPUT_PULLUP);   delayMicroseconds(200); int up = digitalRead(pin);
    pinMode(pin, INPUT_PULLDOWN); delayMicroseconds(200); int down = digitalRead(pin);
    pinMode(pin, INPUT);          // back to high impedance
    const char *state = (up && !down) ? "open" : (!up && !down) ? "low" : (up && down) ? "high" : "odd";
    if (!first) pins += ',';
    pins += "{\"pin\":" + String(pin) + ",\"state\":\"" + state + "\"}";
    first = false;
  }
  for (int pin : ANALOG_PINS) {
    uint32_t mv = analogReadMilliVolts(pin);
    pins += ",{\"mv\":" + String(mv) + ",\"pin\":" + String(pin) + ",\"state\":\"analog\"}";
  }
  pins += "]";

  String i2cUsed = "[";
  first = true;
  for (uint8_t a : I2C_USED) { if (!first) i2cUsed += ','; i2cUsed += String(a); first = false; }
  i2cUsed += "]";

  String used = "[";
  first = true;
  for (const PinUse &u : USED_PINS) {
    if (!first) used += ',';
    used += "{\"pin\":" + String(u.pin) + ",\"use\":\"" + u.use + "\"}";
    first = false;
  }
  used += "]";

  return "{\"ads_ready\":" + String(adsReady ? "true" : "false") +
         ",\"boot_id\":\"" + bootId + "\",\"device_id\":\"PT-01\",\"i2c\":" + i2c +
         ",\"i2c_used\":" + i2cUsed + ",\"pins\":" + pins + ",\"protocol\":\"SMTCS-EDGE/1\",\"sd_ready\":" +
         String(sdReady ? "true" : "false") + ",\"type\":\"HW_REPORT\",\"used\":" + used + "}";
}

void handleHwScan(const String &line) {
  if (!frameCrcValid(line)) return;
  String frame = frameFromCanonical(hwReportCanonical());
  edgeClient.write((const uint8_t *)frame.c_str(), frame.length());
}

// -----------------------------
// Ethernet telemetry only
// -----------------------------
void startEthernet() {
  edgeClient.stop();
  pinMode(ETH_RST, OUTPUT);
  digitalWrite(ETH_RST, LOW);
  delay(50);
  digitalWrite(ETH_RST, HIGH);
  delay(150);

  SPI.begin(ETH_SCK, ETH_MISO, ETH_MOSI, ETH_CS);
  Ethernet.init(ETH_CS);
  byte mac[6] = {0x02, 0x53, 0x4B, 0x50, 0x54, 0x01};
  Ethernet.begin(mac, cfg.deviceIp, cfg.dns, cfg.gateway, cfg.subnet);
  delay(100);
}

void maintainEdgeConnection() {
  if (edgeClient.connected()) return;
  edgeClient.stop();
  uint32_t nowMs = millis();
  if (nowMs - lastEdgeConnectAttemptMs < 1000) return;
  lastEdgeConnectAttemptMs = nowMs;
  if (edgeClient.connect(cfg.receiverIp, cfg.receiverPort)) {
    String hello = makeHelloFrame();
    edgeClient.write((const uint8_t *)hello.c_str(), hello.length());
  }
}

void serviceEdgeReplies() {
  while (edgeClient.connected() && edgeClient.available()) {
    String line = edgeClient.readStringUntil('\n');
    if (line.indexOf("\"type\":\"ACK\"") >= 0) lastEdgeAckMs = millis();
    else if (line.indexOf("\"type\":\"OTA_BEGIN\"") >= 0) handleOtaBegin(line);
    else if (line.indexOf("\"type\":\"HW_SCAN\"") >= 0) handleHwScan(line);
    else ignitionHandleLine(line);
  }
}

void sendPendingBatch() {
  if (batchCount < EDGE_BATCH_SAMPLES) return;
  maintainEdgeConnection();
  if (!edgeClient.connected()) {
    droppedEdgeBatches++;
    batchCount = 0;
    return;
  }
  String frame = makeBatchFrame();
  size_t sent = edgeClient.write((const uint8_t *)frame.c_str(), frame.length());
  if (sent != frame.length()) {
    droppedEdgeBatches++;
    edgeClient.stop();
  } else {
    edgeSequence++;
  }
  batchCount = 0;
}

// -----------------------------
// SD logging — standalone path
// -----------------------------
void startRecording() {
  if (!sdReady || recording) return;
  logFile = SD.open(LOG_PATH, FILE_APPEND);
  if (!logFile) return;
  if (logFile.size() == 0) logFile.println("time_s,voltage_v,pressure_bar");
  sdBuffer.reserve(8192);
  recording = true;
}

void stopRecording() {
  if (!recording) return;
  if (sdBuffer.length()) {
    logFile.print(sdBuffer);
    sdBuffer = "";
  }
  logFile.flush();
  logFile.close();
  recording = false;
}

void flushSdIfNeeded(bool force = false) {
  if (!recording || !logFile) return;
  if (!force && millis() - lastSdFlushMs < 100 && sdBuffer.length() < 4096) return;
  if (sdBuffer.length()) {
    logFile.print(sdBuffer);
    sdBuffer = "";
  }
  logFile.flush();
  lastSdFlushMs = millis();
}

// -----------------------------
// Acquisition
// -----------------------------
void acquireOneSample(uint32_t sampleUs) {
  int16_t raw = ads.readADC_SingleEnded(0);
  float voltage = ads.computeVolts(raw);
  float pressure = (voltage - cfg.zeroVoltage) * cfg.slopeBarPerVolt;
  if (pressure < 0) pressure = 0;

  currentVoltage = voltage;
  currentPressureBar = pressure;
  totalSamples++;

  if (recording) {
    sdBuffer += String(sampleUs / 1000000.0f, 6) + "," + String(voltage, 5) + "," + String(pressure, 4) + "\n";
  }

  if (batchCount == 0) batchFirstSampleUs = sampleUs;
  batchPressureMbar[batchCount] = lroundf(pressure * 1000.0f);
  batchVoltageUv[batchCount] = lroundf(voltage * 1000000.0f);
  batchCount++;
}

// -----------------------------
// Wi-Fi engineering UI
// -----------------------------
String stateJson() {
  String json = "{";
  json += "\"voltage\":" + String(currentVoltage, 5) + ",";
  json += "\"pressure\":" + String(currentPressureBar, 4) + ",";
  json += "\"time\":" + String(millis() / 1000.0f, 2) + ",";
  json += "\"recording\":" + String(recording ? "true" : "false") + ",";
  json += "\"sample_rate_hz\":200,";
  json += "\"zero_voltage\":" + String(cfg.zeroVoltage, 6) + ",";
  json += "\"slope_bar_per_volt\":" + String(cfg.slopeBarPerVolt, 6) + ",";
  json += "\"sd_ready\":" + String(sdReady ? "true" : "false") + ",";
  json += "\"ads_ready\":" + String(adsReady ? "true" : "false") + ",";
  json += "\"ethernet_ip\":\"" + ipToString(cfg.deviceIp) + "\",";
  json += "\"receiver_ip\":\"" + ipToString(cfg.receiverIp) + "\",";
  json += "\"receiver_port\":" + String(cfg.receiverPort) + ",";
  json += "\"edge_connected\":" + String(edgeClient.connected() ? "true" : "false") + ",";
  json += "\"edge_sequence\":" + String(edgeSequence) + ",";
  json += "\"edge_dropped_batches\":" + String(droppedEdgeBatches) + ",";
  json += "\"last_ack_age_ms\":" + String(lastEdgeAckMs ? millis() - lastEdgeAckMs : 0);
  json += "}";
  return json;
}

String htmlPage() {
  return R"HTML(
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pressure Node Engineering</title><style>
body{font-family:system-ui;background:#07151b;color:#d7edf4;margin:0;padding:20px}h1{font-size:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.card{background:#0b2029;border:1px solid #1d4655;padding:16px;border-radius:8px}.metric{font-size:32px;font-weight:700}.muted{color:#7fa5b3;font-size:12px}label{display:block;margin:8px 0}input{width:100%;box-sizing:border-box;padding:8px;background:#07151b;color:#d7edf4;border:1px solid #2b5968}button{padding:9px 14px;margin:4px;border:1px solid #2bc8ed;background:#0b2029;color:#d7edf4}.danger{border-color:#e35b61}pre{white-space:pre-wrap}.ok{color:#59d38c}.bad{color:#e35b61}</style></head>
<body><h1>PT-01 · Pressure Node Engineering / Commissioning</h1><div class="grid">
<div class="card"><div class="muted">LIVE SENSOR</div><div id="pressure" class="metric">—</div><div>bar</div><div id="voltage" class="metric">—</div><div>V</div><p id="status"></p><button onclick="post('/start')">START SD RECORDING</button><button onclick="post('/stop')">STOP</button><button onclick="location='/download'">DOWNLOAD CSV</button></div>
<div class="card"><div class="muted">CALIBRATION</div><label>Zero voltage<input id="zero" type="number" step="0.000001"></label><label>Slope bar/volt<input id="slope" type="number" step="0.000001"></label><button onclick="zeroNow()">ZERO FROM CURRENT VOLTAGE</button><button onclick="saveCalibration()">SAVE CALIBRATION</button><p class="muted">Calibration changes are blocked while SD recording is active.</p></div>
<div class="card"><div class="muted">ETHERNET TELEMETRY</div><label>ESP Ethernet IP<input id="ethip"></label><label>Gateway<input id="gw"></label><label>Subnet<input id="mask"></label><label>DNS<input id="dns"></label><label>Stellar Ops receiver IP<input id="receiver"></label><label>Receiver TCP port<input id="port" type="number"></label><button onclick="saveNetwork()">SAVE & RESTART ETHERNET</button><pre id="netstate"></pre></div>
<div class="card"><div class="muted">DIAGNOSTICS</div><pre id="diag"></pre><button onclick="post('/ethernet/reconnect')">RECONNECT ETHERNET</button></div>
</div><script>
let first=true;async function j(u,o){let r=await fetch(u,o);return r.json()}async function post(u,b={}){return j(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})}
async function tick(){try{let s=await j('/reading');pressure.textContent=Number(s.pressure).toFixed(4);voltage.textContent=Number(s.voltage).toFixed(5);status.textContent=(s.recording?'RECORDING · ':'')+(s.edge_connected?'ETHERNET STREAMING':'ETHERNET DISCONNECTED');status.className=s.edge_connected?'ok':'bad';diag.textContent=`ADS: ${s.ads_ready?'READY':'FAULT'}\nSD: ${s.sd_ready?'READY':'FAULT'}\nSample rate: ${s.sample_rate_hz} Hz\nEdge sequence: ${s.edge_sequence}\nDropped batches: ${s.edge_dropped_batches}\nLast ACK age: ${s.last_ack_age_ms} ms`;netstate.textContent=`ESP: ${s.ethernet_ip}\nReceiver: ${s.receiver_ip}:${s.receiver_port}`;if(first){zero.value=s.zero_voltage;slope.value=s.slope_bar_per_volt;let c=await j('/config');ethip.value=c.ethernet_ip;gw.value=c.gateway;mask.value=c.subnet;dns.value=c.dns;receiver.value=c.receiver_ip;port.value=c.receiver_port;first=false}}catch(e){status.textContent=e.message;status.className='bad'}requestAnimationFrame(()=>setTimeout(tick,50))}async function zeroNow(){let s=await j('/reading');zero.value=s.voltage}async function saveCalibration(){alert((await post('/config/calibration',{zero_voltage:+zero.value,slope:+slope.value})).message)}async function saveNetwork(){alert((await post('/config/network',{ethernet_ip:ethip.value,gateway:gw.value,subnet:mask.value,dns:dns.value,receiver_ip:receiver.value,receiver_port:+port.value})).message)}tick();
</script></body></html>)HTML";
}

void setupWeb() {
  server.on("/", HTTP_GET, []() { server.send(200, "text/html", htmlPage()); });
  server.on("/reading", HTTP_GET, []() { server.send(200, "application/json", stateJson()); });
  server.on("/config", HTTP_GET, []() {
    String json = "{\"ethernet_ip\":\"" + ipToString(cfg.deviceIp) + "\",\"gateway\":\"" + ipToString(cfg.gateway) + "\",\"subnet\":\"" + ipToString(cfg.subnet) + "\",\"dns\":\"" + ipToString(cfg.dns) + "\",\"receiver_ip\":\"" + ipToString(cfg.receiverIp) + "\",\"receiver_port\":" + String(cfg.receiverPort) + "}";
    server.send(200, "application/json", json);
  });
  server.on("/start", HTTP_POST, []() { startRecording(); server.send(200, "application/json", "{\"ok\":true}"); });
  server.on("/stop", HTTP_POST, []() { stopRecording(); server.send(200, "application/json", "{\"ok\":true}"); });
  server.on("/download", HTTP_GET, []() {
    flushSdIfNeeded(true);
    File f = SD.open(LOG_PATH, FILE_READ);
    if (!f) { server.send(404, "text/plain", "No log"); return; }
    server.streamFile(f, "text/csv");
    f.close();
  });
  server.on("/ethernet/reconnect", HTTP_POST, []() { startEthernet(); server.send(200, "application/json", "{\"ok\":true}"); });

  server.on("/config/calibration", HTTP_POST, []() {
    if (recording) { server.send(409, "application/json", "{\"ok\":false,\"message\":\"Stop recording before changing calibration\"}"); return; }
    String body = server.arg("plain");
    int z = body.indexOf("zero_voltage");
    int s = body.indexOf("slope");
    if (z < 0 || s < 0) { server.send(400, "application/json", "{\"ok\":false,\"message\":\"Invalid calibration payload\"}"); return; }
    int zc = body.indexOf(':', z), zEnd = body.indexOf(',', zc);
    int sc = body.indexOf(':', s), sEnd = body.indexOf('}', sc);
    cfg.zeroVoltage = body.substring(zc + 1, zEnd).toFloat();
    cfg.slopeBarPerVolt = body.substring(sc + 1, sEnd).toFloat();
    saveConfig();
    server.send(200, "application/json", "{\"ok\":true,\"message\":\"Calibration saved\"}");
  });

  server.on("/config/network", HTTP_POST, []() {
    String body = server.arg("plain");
    auto stringValue = [&](const char *key) -> String {
      String token = String("\"") + key + "\":\"";
      int p = body.indexOf(token); if (p < 0) return ""; p += token.length(); int e = body.indexOf('"', p); return body.substring(p, e);
    };
    IPAddress ip, gw, mask, dns, receiver;
    if (!parseIp(stringValue("ethernet_ip"), ip) || !parseIp(stringValue("gateway"), gw) || !parseIp(stringValue("subnet"), mask) || !parseIp(stringValue("dns"), dns) || !parseIp(stringValue("receiver_ip"), receiver)) {
      server.send(400, "application/json", "{\"ok\":false,\"message\":\"Invalid IPv4 configuration\"}"); return;
    }
    int pp = body.indexOf("\"receiver_port\":");
    uint16_t port = pp >= 0 ? (uint16_t)body.substring(pp + 16).toInt() : 0;
    if (!port) { server.send(400, "application/json", "{\"ok\":false,\"message\":\"Invalid receiver port\"}"); return; }
    cfg.deviceIp = ip; cfg.gateway = gw; cfg.subnet = mask; cfg.dns = dns; cfg.receiverIp = receiver; cfg.receiverPort = port;
    saveConfig();
    startEthernet();
    server.send(200, "application/json", "{\"ok\":true,\"message\":\"Network configuration saved; Ethernet restarted\"}");
  });

  server.begin();
}

void setup() {
  ignitionSetup();   // relay output LOW before anything else
  Serial.begin(115200);
  loadConfig();
  bootId = String((uint32_t)ESP.getEfuseMac(), HEX) + "-" + String(millis());

  Wire.begin(21, 22);
  adsReady = ads.begin();
  if (adsReady) {
    ads.setGain(GAIN_TWOTHIRDS);
    ads.setDataRate(RATE_ADS1115_860SPS);
  }

  sdSPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  sdReady = SD.begin(SD_CS, sdSPI);

  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  setupWeb();

  startEthernet();
  nextSampleUs = micros();
}

void loop() {
  server.handleClient();
  serviceEdgeReplies();
  ignitionService();   // dead-man, pulse end, status
  maintainEdgeConnection();

  if (!adsReady) {
    delay(10);
    return;
  }

  uint32_t nowUs = micros();
  while ((int32_t)(nowUs - nextSampleUs) >= 0) {
    acquireOneSample(nextSampleUs);
    nextSampleUs += SAMPLE_PERIOD_US;
    nowUs = micros();
    if (batchCount >= EDGE_BATCH_SAMPLES) break;
  }

  sendPendingBatch();
  flushSdIfNeeded(false);
}
