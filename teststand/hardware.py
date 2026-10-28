"""Hardware section: what is physically connected to the ESP32 right now.

The ESP32 answers HW_SCAN with HW_REPORT:
  * i2c       addresses that answer on the I2C bus (SDA 21 / SCL 22)
  * i2c_used  addresses the firmware code uses
  * used      pins the firmware code uses, and what for
  * pins      free pins read as inputs only (never driven):
              open = nothing connected, low/high = something holds the pin,
              analog = input-only pin with its voltage in mV
Anything connected that the code does not use is flagged "NO CODE".
"""
from __future__ import annotations

import threading
import time

from . import events

# Common I2C chips by address (a guess: several chips share an address).
I2C_NAMES = {
    0x1D: "ADXL345 accelerometer", 0x20: "PCF8574 / MCP23017 I/O expander", 0x23: "BH1750 light sensor",
    0x27: "LCD backpack (PCF8574)", 0x29: "VL53L0X distance / TSL2591 light", 0x38: "AHT10/AHT20 temp+humidity",
    0x3C: "OLED display (SSD1306)", 0x3D: "OLED display (SSD1306)",
    0x40: "INA219 current / HTU21D / PCA9685", 0x41: "INA219 current", 0x44: "SHT3x temp+humidity / INA219",
    0x45: "SHT3x temp+humidity / INA219",
    0x48: "ADS1115 / ADS1015 ADC", 0x49: "ADS1115 / ADS1015 ADC", 0x4A: "ADS1115 / ADS1015 ADC",
    0x4B: "ADS1115 / ADS1015 ADC", 0x50: "EEPROM (AT24C)", 0x53: "ADXL345 accelerometer",
    0x57: "EEPROM / MAX30102", 0x5A: "MLX90614 IR thermometer", 0x60: "MCP4725 DAC / Si1145",
    0x62: "MCP4725 DAC", 0x68: "MPU6050 / DS3231 RTC / MAX31865?", 0x69: "MPU6050 (alt address)",
    0x70: "TCA9548A I2C multiplexer", 0x76: "BME280 / BMP280 pressure+temp", 0x77: "BME280 / BMP180 / MS5611",
}
ANALOG_CONNECTED_MV = 100   # an input-only pin above this has a signal on it


class HardwareMonitor:
    def __init__(self, hub):
        self.hub = hub
        self.report: dict | None = None
        self.received = 0.0
        self.requested = 0.0
        self._lock = threading.Lock()
        self._known: set[str] = set()
        hub.add_frame_listener(self._on_frame)

    def _on_frame(self, msg: dict) -> None:
        if msg["type"] == "HELLO":
            with self._lock:
                self.report = None
            self.request()   # scan once every time the ESP32 (re)connects
        elif msg["type"] == "HW_REPORT":
            with self._lock:
                self.report, self.received = msg, time.time()
            self._log_new(self.items())

    def request(self) -> bool:
        self.requested = time.time()
        return self.hub.send({"type": "HW_SCAN", "device_id": "STAND"})

    def _log_new(self, items: list[dict]) -> None:
        flagged = {f"{i['kind']}:{i['where']}" for i in items if i["status"] == "NO CODE"}
        for key in sorted(flagged - self._known):
            item = next(i for i in items if f"{i['kind']}:{i['where']}" == key)
            events.log("HARDWARE", f"New device on {item['where']} — {item['what']} (NO CODE)", "WARN")
        self._known = flagged

    def items(self) -> list[dict]:
        """One row per pin / I2C address with status IN USE, NO CODE, MISSING or FREE."""
        rep = self.report
        if not rep:
            return []
        rows: list[dict] = []
        used_i2c = set(rep.get("i2c_used") or [])
        found = set(rep.get("i2c") or [])
        for addr in sorted(found | used_i2c):
            name = I2C_NAMES.get(addr, "unknown I2C device")
            status = ("IN USE" if addr in found else "MISSING") if addr in used_i2c else "NO CODE"
            rows.append({"kind": "i2c", "where": f"I2C 0x{addr:02X}", "what": name, "status": status,
                         "detail": "answers on SDA 21 / SCL 22" if addr in found else "code expects it, it does not answer"})
        for u in rep.get("used") or []:
            rows.append({"kind": "pin", "where": f"GPIO {u['pin']}", "what": u["use"], "status": "IN USE", "detail": ""})
        for p in rep.get("pins") or []:
            st = p.get("state")
            if st == "analog":
                mv = p.get("mv", 0)
                on = mv >= ANALOG_CONNECTED_MV
                rows.append({"kind": "pin", "where": f"GPIO {p['pin']}", "what": "signal on analog input" if on else "free",
                             "status": "NO CODE" if on else "FREE", "detail": f"{mv} mV"})
            elif st == "open":
                rows.append({"kind": "pin", "where": f"GPIO {p['pin']}", "what": "free", "status": "FREE",
                             "detail": "nothing connected"})
            else:
                what = {"low": "something holds it LOW", "high": "something holds it HIGH"}.get(st, "unstable signal")
                rows.append({"kind": "pin", "where": f"GPIO {p['pin']}", "what": what, "status": "NO CODE",
                             "detail": "a device or wire is connected"})
        return rows

    def summary(self) -> dict:
        rows = self.items()
        return {"report": bool(rows), "no_code": sum(1 for r in rows if r["status"] == "NO CODE"),
                "missing": sum(1 for r in rows if r["status"] == "MISSING")}

    def snapshot(self) -> dict:
        snap = self.hub.snapshot()
        rows = self.items()
        return {"link": snap["link"], "firmware": snap.get("firmware"),
                "supported": "+hw" in (snap.get("firmware") or "") or self.report is not None,
                "received": self.received or None, "items": rows,
                "no_code": sum(1 for r in rows if r["status"] == "NO CODE"),
                "missing": sum(1 for r in rows if r["status"] == "MISSING")}
