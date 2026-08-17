from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .database import add_column


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS recording_sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
 run_id INTEGER,
 source_mode TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT,
 state TEXT NOT NULL, started_by TEXT NOT NULL, sample_count_start INTEGER NOT NULL DEFAULT 0,
 sample_count_stop INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS replay_payloads(
 dataset_id INTEGER PRIMARY KEY, rows_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_runtime(
 operation_id TEXT PRIMARY KEY, dataset_id INTEGER, state TEXT NOT NULL DEFAULT 'STOPPED',
 speed REAL NOT NULL DEFAULT 1, cursor INTEGER NOT NULL DEFAULT 0,
 started_wall_time REAL, started_cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alarm_keys(
 operation_id TEXT NOT NULL, alarm_key TEXT NOT NULL, alarm_id INTEGER NOT NULL,
 PRIMARY KEY(operation_id,alarm_key));
CREATE TABLE IF NOT EXISTS limit_runtime(
 operation_id TEXT NOT NULL, alarm_key TEXT NOT NULL, bad_count INTEGER NOT NULL DEFAULT 0,
 good_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(operation_id,alarm_key));
"""


def ensure_schema(db) -> None:
    db.executescript(RUNTIME_SCHEMA)
    add_column(db,"recording_sessions","run_id INTEGER")


def _channel_maps(db, operation_id: str) -> list[dict]:
    return [dict(row) for row in db.execute("""SELECT c.*,i.raw_field,i.calibration_slope,i.calibration_intercept,
      i.stale_timeout_ms,i.required_for_commit FROM channels c JOIN channel_integrations i
      ON i.operation_id=c.operation_id AND i.channel_id=c.id LEFT JOIN channel_lifecycle l
      ON l.operation_id=c.operation_id AND l.channel_id=c.id
      WHERE c.operation_id=? AND COALESCE(l.enabled,1)=1""", (operation_id,))]


def _named(values: dict, fragment: str, default=0.0):
    for channel_id, item in values.items():
        if fragment in channel_id: return item["value"]
    return default


def _result(mode: str, values: dict, elapsed=0.0, meta=None) -> dict:
    return {"source_mode": mode, "elapsed": round(elapsed, 3),
            "pressure": round(float(_named(values,"pressure")), 3),
            "thrust": round(float(_named(values,"thrust")), 3),
            "temperature": round(float(_named(values,"temperature",28)), 3),
            "continuity": "SAFE", "channels": values, "meta": meta or {}}


def live_snapshot(db, operation_id: str) -> dict:
    session = db.execute("""SELECT s.* FROM edge_sessions s JOIN device_integrations i ON i.device_id=s.device_id
      WHERE i.operation_id=? AND i.enabled=1 AND i.adapter_type='SMTCS_EDGE_TCP'
      ORDER BY s.last_seen DESC LIMIT 1""", (operation_id,)).fetchone()
    maps = _channel_maps(db, operation_id)
    if not session:
        values={m["id"]:{"value":0,"unit":m["unit"],"quality":"DISCONNECTED","age_ms":None} for m in maps}
        unknown = db.execute("SELECT device_id,last_seen FROM edge_sessions ORDER BY last_seen DESC LIMIT 1").fetchone()
        meta={"status":"UNREGISTERED_DEVICE" if unknown else "NO_DEVICE","total_samples":0,"sequence_gaps":0}
        if unknown: meta.update({"device_id":unknown["device_id"],"last_seen":unknown["last_seen"]})
        return _result("LIVE",values,meta=meta)
    received_age_ms=max(0,(time.time()-parse_time(session["last_seen"]))*1000)
    batch = db.execute("SELECT * FROM edge_batches WHERE device_id=? AND boot_id=? ORDER BY sequence DESC LIMIT 1",
                       (session["device_id"],session["boot_id"])).fetchone()
    raw=json.loads(batch["channels_json"]) if batch else {}
    values={}
    for m in maps:
        samples=raw.get(m["raw_field"],[]); quality="GOOD"
        if session["status"]=="DISCONNECTED": quality="DISCONNECTED"
        elif received_age_ms>m["stale_timeout_ms"]: quality="STALE"
        elif not samples: quality="INVALID"
        value=(float(samples[-1])*m["calibration_slope"]+m["calibration_intercept"]) if samples else 0
        if m["critical"] is not None and value>=m["critical"]: quality="OUT_OF_RANGE"
        values[m["id"]]={"value":value,"raw":samples[-1] if samples else None,"unit":m["unit"],"quality":quality,
                         "age_ms":round(received_age_ms,1),"source":session["device_id"]}
    elapsed=(batch["first_sample_us"]+(batch["sample_count"]-1)*batch["sample_period_us"])/1_000_000 if batch else 0
    return _result("LIVE",values,elapsed,{"status":session["status"],"device_id":session["device_id"],
      "boot_id":session["boot_id"],"last_sequence":session["last_sequence"],"total_samples":session["total_samples"],
      "sequence_gaps":session["sequence_gaps"],"age_ms":round(received_age_ms,1)})


def replay_snapshot(db, operation_id: str) -> dict:
    runtime=db.execute("SELECT * FROM replay_runtime WHERE operation_id=?",(operation_id,)).fetchone()
    maps=_channel_maps(db,operation_id)
    if not runtime or not runtime["dataset_id"]:
        return _result("REPLAY",{m["id"]:{"value":0,"unit":m["unit"],"quality":"INVALID","age_ms":None} for m in maps},meta={"status":"NO_DATASET"})
    payload=db.execute("SELECT rows_json FROM replay_payloads WHERE dataset_id=?",(runtime["dataset_id"],)).fetchone()
    rows=json.loads(payload["rows_json"]) if payload else []
    cursor=runtime["cursor"]
    if runtime["state"]=="PLAYING" and runtime["started_wall_time"] is not None:
        cursor=min(len(rows)-1, runtime["started_cursor"]+int((time.time()-runtime["started_wall_time"])*runtime["speed"]*20)) if rows else 0
    row=rows[cursor] if rows else {}
    values={}
    for m in maps:
        raw=row.get(m["raw_field"]); quality="REPLAY"
        try: value=float(raw)*m["calibration_slope"]+m["calibration_intercept"]
        except (TypeError,ValueError): value=0; quality="INVALID"
        values[m["id"]]={"value":value,"raw":raw,"unit":m["unit"],"quality":quality,"age_ms":0,"source":"CSV_REPLAY"}
    return _result("REPLAY",values,cursor/20,{"status":runtime["state"],"cursor":cursor,"row_count":len(rows),"speed":runtime["speed"]})


def runtime_snapshot(db, operation: dict, simulation: dict) -> dict:
    ensure_schema(db); mode=operation["mode"]
    if mode=="LIVE": return live_snapshot(db,operation["id"])
    if mode=="REPLAY": return replay_snapshot(db,operation["id"])
    values={}
    for channel in _channel_maps(db,operation["id"]):
        value = simulation["pressure"] if "pressure" in channel["id"] else simulation["thrust"] if "thrust" in channel["id"] else simulation["temperature"] if "temperature" in channel["id"] else 0
        values[channel["id"]]={"value":value,"unit":channel["unit"],"quality":"SIMULATED","age_ms":0}
    result=_result("SIMULATION",values,simulation["elapsed"],{"status":"SIMULATED"}); result["continuity"]=simulation["continuity"]; return result


def recording_status(db, operation_id: str) -> dict:
    ensure_schema(db); run=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(operation_id,)).fetchone()
    if run: db.execute("UPDATE recording_sessions SET run_id=? WHERE operation_id=? AND run_id IS NULL",(run["id"],operation_id))
    row=db.execute("SELECT * FROM recording_sessions WHERE operation_id=? AND run_id IS ? ORDER BY id DESC LIMIT 1",(operation_id,run["id"] if run else None)).fetchone()
    return dict(row) if row else {"state":"STOPPED","id":None,"source_mode":None}


def evaluate_alarms(db, operation_id: str, telemetry: dict) -> None:
    ensure_schema(db)
    operation=db.execute("SELECT state FROM operations WHERE id=?",(operation_id,)).fetchone()
    profile=db.execute("SELECT settings_json FROM limit_profiles WHERE operation_id=? AND enabled=1 AND phase=? ORDER BY name LIMIT 1",(operation_id,operation["state"] if operation else "CHECKOUT")).fetchone()
    settings=json.loads(profile["settings_json"]) if profile else {"persistence_samples":1,"hysteresis_percent":2}
    persistence=max(1,int(settings.get("persistence_samples",1)))
    limits={row["id"]:dict(row) for row in db.execute("SELECT id,warning,critical FROM channels WHERE operation_id=?",(operation_id,))}
    run=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(operation_id,)).fetchone()

    def transition(key: str, bad: bool, priority: str, source: str, message: str, required: int = 1) -> None:
        state=db.execute("SELECT * FROM limit_runtime WHERE operation_id=? AND alarm_key=?",(operation_id,key)).fetchone()
        bad_count=(state["bad_count"] if state else 0)+1 if bad else 0; good_count=0 if bad else (state["good_count"] if state else 0)+1
        db.execute("""INSERT INTO limit_runtime VALUES(?,?,?,?) ON CONFLICT(operation_id,alarm_key)
            DO UPDATE SET bad_count=excluded.bad_count,good_count=excluded.good_count""",(operation_id,key,bad_count,good_count))
        existing=db.execute("SELECT alarm_id FROM alarm_keys WHERE operation_id=? AND alarm_key=?",(operation_id,key)).fetchone()
        if bad and bad_count>=required and not existing:
            cur=db.execute("INSERT INTO alarms(operation_id,opened_at,priority,source,message,state,run_id) VALUES(?,?,?,?,?,'ACTIVE_UNACKNOWLEDGED',?)",
                           (operation_id,utc_now(),priority,source,message,run["id"] if run else None))
            db.execute("INSERT INTO alarm_keys VALUES(?,?,?)",(operation_id,key,cur.lastrowid))
        elif not bad and existing and good_count>=required:
            db.execute("UPDATE alarms SET state='CLOSED' WHERE id=?",(existing["alarm_id"],)); db.execute("DELETE FROM alarm_keys WHERE operation_id=? AND alarm_key=?",(operation_id,key))

    for channel_id,item in telemetry.get("channels",{}).items():
        quality_bad=item["quality"] in {"STALE","DISCONNECTED","INVALID","UNCALIBRATED"}
        transition(f"QUALITY:{channel_id}",quality_bad,"P2",channel_id,f"Channel quality is {item['quality']}",1)
        definition=limits.get(channel_id,{})
        value=float(item.get("value",0)); critical=definition.get("critical"); warning=definition.get("warning")
        critical_bad=critical is not None and value>=critical
        warning_bad=not critical_bad and warning is not None and value>=warning
        transition(f"LIMIT_CRITICAL:{channel_id}",critical_bad,"P1",channel_id,f"Value {value:g} exceeds critical high limit {critical:g}" if critical is not None else "Critical limit cleared",persistence)
        transition(f"LIMIT_WARNING:{channel_id}",warning_bad,"P2",channel_id,f"Value {value:g} exceeds warning high limit {warning:g}" if warning is not None else "Warning limit cleared",persistence)
