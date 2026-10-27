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


# ---------------- updater ----------------
def _run(*cmd, cwd=None):
    import subprocess
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def test_updater_detects_and_applies_update(tmp_path, monkeypatch):
    from teststand import updater
    bare, work, dev = tmp_path / "remote.git", tmp_path / "install", tmp_path / "dev"
    _run("git", "init", "-q", "--bare", "-b", "teststand/console", str(bare))
    _run("git", "clone", "-q", str(bare), str(dev))
    for repo in (dev,):
        _run("git", "config", "user.email", "t@t", cwd=repo); _run("git", "config", "user.name", "t", cwd=repo)
    (dev / "VERSION").write_text("1.0.0\n")
    _run("git", "checkout", "-q", "-b", "teststand/console", cwd=dev)
    _run("git", "add", "-A", cwd=dev); _run("git", "commit", "-qm", "v1", cwd=dev)
    _run("git", "push", "-q", "origin", "teststand/console", cwd=dev)
    _run("git", "clone", "-q", "-b", "teststand/console", str(bare), str(work))
    (work / "config.yaml").write_text("local settings\n")  # untracked, must survive
    monkeypatch.setattr(updater, "ROOT", work)
    monkeypatch.setattr(updater, "VERSION_FILE", work / "VERSION")

    assert updater.check()["update_available"] is False
    (dev / "VERSION").write_text("1.1.0\n")
    _run("git", "commit", "-qam", "New feature", cwd=dev)
    _run("git", "push", "-q", "origin", "teststand/console", cwd=dev)

    info = updater.check()
    assert info["update_available"] and info["behind"] == 1 and info["remote_version"] == "1.1.0"
    assert info["changes"][0]["title"] == "New feature"
    result = updater.apply()
    assert result["version"] == "1.1.0"
    assert (work / "config.yaml").read_text() == "local settings\n"
    assert updater.check()["update_available"] is False


def test_update_refused_while_armed(tmp_path):
    from teststand.app import System
    from teststand.web.server import create_app
    cfg = load_config(None)
    cfg["telemetry"].update(listen_host="127.0.0.1", listen_port=0)
    cfg["recorder"]["directory"] = str(tmp_path)
    system = System(cfg)
    system.ignition.state = "ARMED"
    client = create_app(system).test_client()
    r = client.post("/api/update/apply")
    assert r.status_code == 409 and "SAFE" in r.get_json()["error"]
    r = client.post("/api/system/shutdown")
    assert r.status_code == 409
    system.ignition.state = "SAFE"


def test_example_config_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    assert cfg["ignition"]["driver"] == "simulation"
    assert all(c["url"] for c in cfg["cameras"])


# ---------------- v1.1: relay test, settings, system check, port in use ----------------
def test_relay_test_rules_simulation():
    ctl = IgnitionController(ign_cfg(), SimulationDriver(), link_ok=lambda: True)
    ctl.start()
    assert ctl.test_relay("arm") == 300
    with pytest.raises(IgnitionError):
        ctl.test_relay("both")
    ctl.arm()
    with pytest.raises(IgnitionError):
        ctl.test_relay("fire")   # only from SAFE
    ctl.stop()


def test_relay_test_reaches_fake_esp32(hub):
    node = FakeNode("127.0.0.1", hub.port)
    threading.Thread(target=node.run, daemon=True).start()
    assert wait_for(hub.link_ok, 5)
    driver = Esp32Driver(hub, "PT-01")
    ctl = IgnitionController(ign_cfg(), driver, link_ok=hub.link_ok)
    ctl.start()
    ctl.test_relay("fire")
    assert wait_for(lambda: node.test_relays == ["fire"], 2)
    assert node.fire_count == 0
    assert wait_for(lambda: driver.hardware_state()["module"] and driver.hardware_state()["ads_ready"], 3)
    ctl.stop(); node.stop()


def test_port_in_use_is_reported_not_fatal():
    import socket as so
    blocker = so.socket(); blocker.bind(("127.0.0.1", 0)); blocker.listen()
    port = blocker.getsockname()[1]
    h = TelemetryHub(dict(DEFAULTS["telemetry"], listen_host="127.0.0.1", listen_port=port))
    h.start()
    assert h.snapshot()["port_error"] and h.snapshot()["link"] == "OFFLINE"
    blocker.close()
    assert wait_for(lambda: h.snapshot()["port_error"] is None, 8)
    h.stop()


def _system(tmp_path, extra=None):
    from teststand.app import System
    import yaml
    path = tmp_path / "config.yaml"
    data = {"telemetry": {"listen_host": "127.0.0.1", "listen_port": 0},
            "recorder": {"directory": str(tmp_path / "rec")}, **(extra or {})}
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    return System(cfg, path), path


def test_settings_save_cameras_and_password_kept(tmp_path, monkeypatch):
    from teststand import updater
    from teststand.web.server import create_app
    import yaml
    system, path = _system(tmp_path)
    monkeypatch.setattr(updater, "restart_later", lambda **kw: None)
    client = create_app(system).test_client()
    body = {"ignition": {"countdown_s": "7", "driver": "esp32"},
            "cameras": [{"id": "c1", "name": "STAND", "enabled": True, "ip": "192.168.1.60", "brand": "tapo",
                         "user": "admin", "password": "s3cret"}]}
    r = client.post("/api/settings", json=body)
    assert r.get_json()["ok"], r.get_json()
    saved = yaml.safe_load(path.read_text())
    assert saved["ignition"]["countdown_s"] == 7 and saved["cameras"][0]["password"] == "s3cret"
    cfg = load_config(path)
    assert cfg["cameras"][0]["record_url"] == "rtsp://admin:s3cret@192.168.1.60:554/stream1"
    assert cfg["cameras"][0]["preview_url"].endswith("/stream2")
    # page never receives the password, and saving it back unchanged keeps it
    got = client.get("/api/settings").get_json()["settings"]["cameras"][0]
    assert got["password"] == "********"
    r = client.post("/api/settings", json={"cameras": [got]})
    assert r.get_json()["ok"]
    assert yaml.safe_load(path.read_text())["cameras"][0]["password"] == "s3cret"
    # invalid settings are refused and not written
    r = client.post("/api/settings", json={"ignition": {"driver": "gpio"}})
    assert r.status_code == 400
    assert yaml.safe_load(path.read_text())["ignition"]["driver"] == "esp32"


def test_settings_refused_while_armed(tmp_path):
    from teststand.web.server import create_app
    system, _ = _system(tmp_path)
    system.ignition.state = "ARMED"
    r = create_app(system).test_client().post("/api/settings", json={})
    assert r.status_code == 409
    system.ignition.state = "SAFE"


def test_system_check_runs(tmp_path):
    from teststand.web.server import create_app
    system, _ = _system(tmp_path, {"telemetry": {"listen_host": "127.0.0.1", "listen_port": 0, "esp_ip": "127.0.0.1"}})
    system.hub.start()
    j = create_app(system).test_client().get("/api/check").get_json()
    ids = {c["id"]: c for c in j["checks"]}
    assert {"network", "port", "firewall", "esp_ping", "esp_link", "adc", "ignition", "disk"} <= set(ids)
    assert ids["esp_link"]["status"] == "FAIL" and ids["esp_link"]["fix"]
    assert j["summary"] == "FAIL"
    system.hub.stop()


def test_camera_test_with_local_video(tmp_path):
    import subprocess
    from teststand.cameras.camera import ffmpeg_path
    from teststand.web.server import create_app
    if not ffmpeg_path():
        pytest.skip("no ffmpeg")
    clip = tmp_path / "c.mp4"
    subprocess.run([ffmpeg_path(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                    "-t", "1", str(clip)], check=True)
    system, _ = _system(tmp_path)
    client = create_app(system).test_client()
    j = client.post("/api/cameras/test", json={"url": str(clip)}).get_json()
    assert j["ok"] and j["image"].startswith("data:image/jpeg;base64,")
    j = client.post("/api/cameras/test", json={"url": str(tmp_path / "missing.mp4")}).get_json()
    assert not j["ok"]
