# ESP32 Pressure Node

This firmware separates **engineering commissioning** from **operational telemetry**.

## Wi-Fi — standalone engineering interface

The ESP32 continues to create the access point:

- SSID: `PressureMonitor`
- Password: `12345678`
- UI: `http://192.168.4.1`

The Wi-Fi interface is intentionally local to the pressure node. It provides:

- current voltage and pressure
- 200 Hz acquisition status
- ADS1115 and SD diagnostics
- SD start/stop/download
- calibration zero voltage and slope
- zero-from-current-voltage action
- Ethernet static IP / gateway / subnet / DNS
- Test-stand console receiver IP and TCP port
- Ethernet reconnect
- sequence, dropped-batch and ACK diagnostics

Calibration changes are blocked while local SD recording is running.
Configuration is persisted in ESP32 NVS/Preferences.

The legacy-compatible endpoints remain available:

- `GET /reading`
- `POST /start`
- `POST /stop`
- `GET /download`

## Ethernet — operational telemetry only

Ethernet does not host an operator UI. It pushes pressure telemetry to Stellar Ops over a persistent TCP connection using `SMTCS-EDGE/1`.

Default network values:

- ESP Ethernet IP: `192.168.1.50`
- Stellar Ops receiver: `192.168.1.10`
- TCP port: `9100`

The firmware acquires at 200 Hz and transmits 20-sample batches every ~100 ms. Samples on the wire are integer-scaled for deterministic CRC framing:

- `pressure_mbar`
- `voltage_uv`

Stellar Ops converts `pressure_mbar` to engineering `bar` using the channel mapping (`slope = 0.001`). The ESP's local SD logging remains independent of the network path.

## Hardware pins

### ADS1115
- SDA: GPIO21
- SCL: GPIO22

### SD (HSPI)
- SCK: GPIO18
- MISO: GPIO19
- MOSI: GPIO23
- CS: GPIO5

### W5100 Ethernet (separate SPI)
- SCK: GPIO25
- MISO: GPIO26
- MOSI: GPIO27
- CS: GPIO32
- RST: GPIO33

## Required Arduino libraries

- Adafruit ADS1X15
- Ethernet
- built-in ESP32 WiFi / WebServer / Preferences / SD / SPI

## Stellar Ops side

When Stellar Ops starts it now:

1. binds `PT-01` to inbound `SMTCS_EDGE_TCP`
2. maps `motor.chamber_pressure` to `pressure_mbar`
3. starts the TCP edge listener on port 9100
4. receives and persists pushed edge batches
5. exposes the data through the existing LIVE telemetry runtime

The previous HTTP `/reading` polling path is no longer used by the operational application startup.

## Ignition module — `ignition.ino`

The sketch folder has two tabs. `esp32_pressure_node.ino` is the pressure node; `ignition.ino` is the ignition module. The main sketch only calls three functions: `ignitionSetup()` (first line of `setup()`), `ignitionHandleLine()` (each line received from the console) and `ignitionService()` (every `loop()`).

Commands from the console (same TCP link, `SMTCS-EDGE/1`, CRC32 checked): `IGN_ARM` (also the keep-alive, every 300 ms), `IGN_FIRE` (`pulse_ms`), `IGN_SAFE`, and `IGN_TEST` (`relay`: `arm` or `fire`, `pulse_ms` ≤ 1000) for the RELAY TEST buttons. The node answers with `IGN_STATUS` (`SAFE` / `ARMED` / `FIRING` / `TEST`, `fire_count`, `ads_ready`, `sd_ready`) every 500 ms and on every change.

Relay test rules: only from SAFE, one relay at a time, max 1000 ms. A FIRE-relay test is refused when `IGN_ARM_PIN` is `-1` (no arm relay), because then one relay alone would fire the igniter.

| Setting | Default | Meaning |
|---|---|---|
| `IGN_FIRE_PIN` | GPIO 4 | fire relay / MOSFET, HIGH during the pulse |
| `IGN_ARM_PIN` | GPIO 13 | arming relay, HIGH while armed (`-1` = not used) |
| `IGN_DEADMAN_MS` | 1500 | no keep-alive for this long → SAFE |
| `IGN_MAX_PULSE_MS` | 3000 | longest allowed pulse |

**Before the first live test:** confirm the pins match the wiring, then run the full sequence with a lamp or LED in place of the igniter.
