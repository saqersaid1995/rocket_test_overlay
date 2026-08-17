from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def active_run(db, operation_id: str):
    return db.execute("SELECT * FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(operation_id,)).fetchone()


def open_package(db, operation_id: str, recording_session_id: int, root: Path) -> int:
    run=active_run(db,operation_id)
    if not run:
        raise ValueError("an active test run is required before recording")
    directory=root/"evidence"/run["code"]/f"recording-{recording_session_id:06d}"
    directory.mkdir(parents=True,exist_ok=True)
    cursor=db.execute("""INSERT INTO evidence_packages(operation_id,run_id,recording_session_id,created_at,state,directory)
        VALUES(?,?,?,?,'OPEN',?)""",(operation_id,run["id"],recording_session_id,utc_now(),str(directory)))
    return cursor.lastrowid


def close_package(db, operation_id: str, recording_session_id: int) -> dict:
    package=db.execute("SELECT * FROM evidence_packages WHERE operation_id=? AND recording_session_id=? AND state='OPEN' ORDER BY id DESC LIMIT 1",(operation_id,recording_session_id)).fetchone()
    if not package:
        raise ValueError("open evidence package was not found")
    run=db.execute("SELECT * FROM test_runs WHERE id=?",(package["run_id"],)).fetchone()
    session=db.execute("SELECT started_at,stopped_at FROM recording_sessions WHERE id=?",(recording_session_id,)).fetchone()
    batch=db.execute("""SELECT count(*) batches,COALESCE(sum(sample_count),0) samples FROM edge_batches
        WHERE run_id=? AND received_at>=? AND received_at<=?""",(package["run_id"],session["started_at"],session["stopped_at"])).fetchone()
    sequences=db.execute("""SELECT device_id,boot_id,sequence FROM edge_batches WHERE run_id=?
        AND received_at>=? AND received_at<=? ORDER BY device_id,boot_id,sequence""",(package["run_id"],session["started_at"],session["stopped_at"])).fetchall()
    gaps=0; previous={}
    for row in sequences:
        key=(row["device_id"],row["boot_id"]); last=previous.get(key)
        if last is not None: gaps+=max(0,row["sequence"]-last-1)
        previous[key]=row["sequence"]
    telemetry_path=Path(package["directory"])/"telemetry.jsonl"; telemetry_hash=hashlib.sha256()
    telemetry_rows=db.execute("""SELECT device_id,boot_id,sequence,received_at,first_sample_us,sample_period_us,sample_count,channels_json
        FROM edge_batches WHERE run_id=? AND received_at>=? AND received_at<=? ORDER BY id""",(package["run_id"],session["started_at"],session["stopped_at"])).fetchall()
    with telemetry_path.open("wb") as output:
        for row in telemetry_rows:
            record=dict(row); record["channels"]=json.loads(record.pop("channels_json")); line=json.dumps(record,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("utf-8")+b"\n"
            output.write(line); telemetry_hash.update(line)
    manifest={
        "schema":"SMTCS-EVIDENCE/1",
        "created_at":package["created_at"],"closed_at":utc_now(),
        "operation_id":operation_id,"run":{"id":run["id"],"code":run["code"],"title":run["title"],"test_article":run["test_article"],"configuration_revision":run["configuration_revision"],"propellant_batch":run["propellant_batch"]},
        "recording_session_id":recording_session_id,
        "telemetry":{"batch_count":batch["batches"],"sample_count":batch["samples"],"sequence_gaps":gaps,
                     "file":"telemetry.jsonl","file_sha256":telemetry_hash.hexdigest()},
        "events":db.execute("SELECT count(*) FROM events WHERE run_id=?",(run["id"],)).fetchone()[0],
        "alarms":db.execute("SELECT count(*) FROM alarms WHERE run_id=?",(run["id"],)).fetchone()[0],
    }
    payload=json.dumps(manifest,indent=2,sort_keys=True).encode("utf-8")
    digest=hashlib.sha256(payload).hexdigest(); manifest_path=Path(package["directory"])/"manifest.json"
    manifest_path.write_bytes(payload)
    db.execute("""UPDATE evidence_packages SET closed_at=?,state='SEALED',manifest_path=?,manifest_sha256=?,
        telemetry_batches=?,telemetry_samples=?,sequence_gaps=? WHERE id=?""",(manifest["closed_at"],str(manifest_path),digest,batch["batches"],batch["samples"],gaps,package["id"]))
    return {"package_id":package["id"],"manifest_path":str(manifest_path),"sha256":digest,**manifest["telemetry"]}
