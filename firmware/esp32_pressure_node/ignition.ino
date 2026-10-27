// ============================================================================
//  IGNITION MODULE  (separate tab of esp32_pressure_node)
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
//    * Relay test (IGN_TEST): from SAFE only, ONE relay at a time, max 1000 ms.
//      A FIRE-relay test is refused when no arm relay is configured, because a
//      single fire relay would then fire the igniter.
//
//  !! CHECK IGN_ARM_PIN / IGN_FIRE_PIN AGAINST YOUR WIRING BEFORE UPLOADING !!
//  !! First test with an LED or a lamp instead of an igniter.              !!
// ============================================================================

static const int IGN_FIRE_PIN = 4;     // free GPIO on this board (not SD / Ethernet / I2C)
static const int IGN_ARM_PIN  = 13;    // set to -1 if you have no arming relay
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

// Verifies the CRC32 of a frame sent by the console (crc32 is the last key).
bool ignFrameValid(const String &line) {
  int idx = line.lastIndexOf(",\"crc32\":\"");
  if (idx < 0 || line.length() < (unsigned)(idx + 18)) return false;
  String canonical = line.substring(0, idx) + "}";
  String supplied = line.substring(idx + 10, idx + 18);
  supplied.toLowerCase();
  return supplied == crcHex(canonical);
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
  if (!ignFrameValid(line)) { Serial.println("[IGN] frame rejected: CRC"); return; }
  if (line.indexOf(String("\"target\":\"") + IGN_DEVICE_ID + "\"") < 0) return;

  if (line.indexOf("\"type\":\"IGN_TEST\"") >= 0) {
    bool fireRelay = line.indexOf("\"relay\":\"fire\"") >= 0;
    bool armRelay = line.indexOf("\"relay\":\"arm\"") >= 0;
    int pin = fireRelay ? IGN_FIRE_PIN : (armRelay ? IGN_ARM_PIN : -1);
    if (ignState != IGN_SAFE_STATE || pin < 0 || (fireRelay && IGN_ARM_PIN < 0)) {
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
