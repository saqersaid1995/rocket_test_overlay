import json
import threading
import time
import zlib

import pytest

from teststand.config import DEFAULTS, load_config
from teststand.ignition.controller import COUNTDOWN, FIRING, SAFE, IgnitionController, IgnitionError
from teststand.ignition.drivers import Esp32Driver, SimulationDriver
from teststand.recorder.session import Recorder
from teststand.telemetry.protocol import ProtocolError, decode, encode
from teststand.telemetry.receiver import TelemetryHub
from teststand.telemetry.simulator import FakeNode


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def ign_cfg(**kw):
    cfg = dict(DEFAULTS["ignition"], countdown_s=0.4, pulse_ms=200, arm_timeout_s=30, keepalive_ms=100)
    cfg.update(kw)
    return cfg


# ---------------- protocol ----------------
def test_protocol_roundtrip_and_crc():
    frame = encode({"type": "HELLO", "device_id": "PT-01", "boot_id": "x"})
    assert decode(frame)["type"] == "HELLO"
    bad = frame.replace(b"PT-01", b"PT-02")
    with pytest.raises(ProtocolError):
        decode(bad)


def test_command_frame_verifiable_like_esp32():
    """Re-implements the ESP32 check in ignition.ino: cut ',"crc32":"…"' and CRC the rest."""
    line = encode({"type": "IGN_FIRE", "device_id": "STAND", "target": "PT-01", "seq": 7, "pulse_ms": 1000})
    text = line.decode().rstrip("\n")
    idx = text.rfind(',"crc32":"')
    canonical = text[:idx] + "}"
    supplied = text[idx + 10:idx + 18]
    assert supplied == f"{zlib.crc32(canonical.encode()) & 0xFFFFFFFF:08x}"
    assert '"type":"IGN_FIRE"' in text and '"target":"PT-01"' in text and '"pulse_ms":1000' in text


def test_esp32_status_frame_format_is_accepted():
    # exactly what ignSendStatus() builds in the firmware
    canonical = ('{"boot_id":"abc-1","device_id":"PT-01","fire_count":2,'
                 '"protocol":"SMTCS-EDGE/1","state":"ARMED","type":"IGN_STATUS"}')
    crc = f"{zlib.crc32(canonical.encode()) & 0xFFFFFFFF:08x}"
    line = canonical[:-1] + f',"crc32":"{crc}"}}\n'
    msg = decode(line.encode())
    assert msg["state"] == "ARMED" and msg["fire_count"] == 2


# ---------------- ignition state machine ----------------
def test_arm_blocked_without_telemetry():
    ctl = IgnitionController(ign_cfg(), SimulationDriver(), link_ok=lambda: False)
    ctl.start()
    with pytest.raises(IgnitionError):
        ctl.arm()
    assert ctl.state == SAFE
    ctl.stop()


def test_full_sequence_fires_once_and_returns_safe():
    fired = []
    ctl = IgnitionController(ign_cfg(), SimulationDriver(), link_ok=lambda: True)
    ctl.on("fire", fired.append)
    ctl.start()
    with pytest.raises(IgnitionError):
        ctl.start_countdown()  # not armed
    ctl.arm()
    ctl.start_countdown()
    assert ctl.state == COUNTDOWN
    assert wait_for(lambda: ctl.state == FIRING, 2)
    assert wait_for(lambda: ctl.state == SAFE, 2)
    assert len(fired) == 1
    ctl.stop()


def test_abort_during_countdown_never_fires():
    fired = []
    ctl = IgnitionController(ign_cfg(countdown_s=0.5), SimulationDriver(), link_ok=lambda: True)
    ctl.on("fire", fired.append)
    ctl.start()
    ctl.arm()
    ctl.start_countdown()
    ctl.abort()
    time.sleep(0.8)
    assert ctl.state == SAFE and not fired
    ctl.stop()


def test_link_loss_aborts_countdown():
    link = {"ok": True}
    fired = []
    ctl = IgnitionController(ign_cfg(countdown_s=1), SimulationDriver(), link_ok=lambda: link["ok"])
    ctl.on("fire", fired.append)
    ctl.start()
    ctl.arm()
    ctl.start_countdown()
    link["ok"] = False
    assert wait_for(lambda: ctl.state == SAFE, 1)
    assert "telemetry" in ctl.last_abort_reason
    time.sleep(1.2)
    assert not fired
    ctl.stop()


def test_arm_timeout():
    ctl = IgnitionController(ign_cfg(arm_timeout_s=0.3), SimulationDriver(), link_ok=lambda: True)
    ctl.start()
    ctl.arm()
    assert wait_for(lambda: ctl.state == SAFE, 2)
    assert ctl.last_abort_reason == "arm timeout"
    ctl.stop()


# ---------------- end to end with the fake ESP32 ----------------
@pytest.fixture
def hub():
    cfg = dict(DEFAULTS["telemetry"], listen_host="127.0.0.1", listen_port=0)
    h = TelemetryHub(cfg)
    h.start()
    yield h
    h.stop()


def test_telemetry_and_esp32_fire_path(hub):
    node = FakeNode("127.0.0.1", hub.port, burn_s=0.5, peak_bar=40)
    threading.Thread(target=node.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    assert wait_for(lambda: hub.snapshot()["samples"] >= 100, 3)
    snap = hub.snapshot()
    assert snap["link"] == "LIVE" and snap["rate_hz"] == 200.0

    driver = Esp32Driver(hub, "PT-01")
    ctl = IgnitionController(ign_cfg(countdown_s=0.5), driver, link_ok=hub.link_ok)
    ctl.start()
    ctl.arm()
    assert wait_for(lambda: node.state == "ARMED", 2)
    assert wait_for(lambda: driver.hardware_state()["state"] == "ARMED", 2)
    ctl.start_countdown()
    assert wait_for(lambda: node.fire_count == 1, 3)
    assert wait_for(lambda: hub.peak_bar > 30, 3), hub.peak_bar
    assert wait_for(lambda: ctl.state == SAFE and node.state == "SAFE", 3)
    ctl.stop()
    node.stop()


def test_esp32_deadman_when_keepalive_stops(hub):
    node = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=node.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    driver = Esp32Driver(hub, "PT-01")
    driver.arm()  # one ARM, then silence (PC crashed)
    assert wait_for(lambda: node.state == "ARMED", 2)
    assert wait_for(lambda: node.state == "SAFE", 3)
    node.stop()


def test_fire_refused_by_node_when_not_armed(hub):
    node = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=node.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    Esp32Driver(hub, "PT-01").fire(500)
    time.sleep(0.5)
    assert node.fire_count == 0
    node.stop()


# ---------------- recorder ----------------
class NoCams:
    def start_recording(self, d): pass
    def stop_recording(self): pass
    def snapshot(self): return []


def test_recorder_writes_pressure_and_summary(hub, tmp_path):
    node = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=node.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    time.sleep(0.5)
    rec = Recorder(dict(DEFAULTS["recorder"], directory=str(tmp_path)), hub, NoCams())
    session = rec.start("test")
    time.sleep(0.6)
    summary = rec.stop("test")
    rows = (session / "pressure.csv").read_text().splitlines()
    assert rows[0].startswith("local_time")
    assert len(rows) > 150  # pre-trigger + live samples
    assert json.loads((session / "summary.json").read_text())["samples"] == summary["samples"]
    assert "Recording started" in (session / "events.log").read_text()
    node.stop()


# ---------------- web ----------------
def test_web_api(tmp_path):
    from teststand.app import System
    from teststand.web.server import create_app
    cfg = load_config(None)
    cfg["telemetry"].update(listen_host="127.0.0.1", listen_port=0)
    cfg["recorder"]["directory"] = str(tmp_path)
    system = System(cfg)
    system.start()
    client = create_app(system).test_client()
    assert client.get("/").status_code == 200
    s = client.get("/api/state").get_json()
    assert s["ignition"]["state"] == "SAFE" and s["telemetry"]["link"] == "OFFLINE"
    r = client.post("/api/ignition/arm")
    assert r.status_code == 409 and "telemetry" in r.get_json()["error"]
    assert client.post("/api/ignition/abort").get_json()["ok"]
    system.stop()


def test_config_clamps_pulse_and_rejects_bad_driver(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("ignition: {pulse_ms: 99999, countdown_s: 0}\n")
    cfg = load_config(p)
    assert cfg["ignition"]["pulse_ms"] == 3000 and cfg["ignition"]["countdown_s"] == 3
    p.write_text("ignition: {driver: gpio}\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_second_node_with_same_id_is_rejected(hub):
    a = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=a.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    time.sleep(0.3)
    b = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=b.run, daemon=True).start()
    time.sleep(1.5)
    snap = hub.snapshot()
    assert snap["link"] == "LIVE" and snap["gaps"] == 0
    a.stop(); b.stop()
