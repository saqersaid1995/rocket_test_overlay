from __future__ import annotations

import os
import shutil
import time

from flask import Flask, redirect, url_for

from .control import CONTROL_DB, OPERATION_ID, connect, control, init_control_db
from .telemetry_runtime import recording_status
from .media import media

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("STELLAR_OPS_SECRET", "development-only-change-me")
app.register_blueprint(control)
app.register_blueprint(media)


@app.get("/")
def home():
    return redirect(url_for("control.workspace_console"))


@app.get("/health")
def health():
    init_control_db()
    with connect() as db:
        db_started=time.monotonic(); db.execute("SELECT 1").fetchone(); latency=round((time.monotonic()-db_started)*1000,2)
        operation=db.execute("SELECT mode,state FROM operations WHERE id=?",(OPERATION_ID,)).fetchone()
        edge=db.execute("SELECT status,last_seen,total_samples,sequence_gaps FROM edge_sessions ORDER BY last_seen DESC LIMIT 1").fetchone()
        run=db.execute("SELECT id,code,status FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(OPERATION_ID,)).fetchone()
        recording=recording_status(db,OPERATION_ID)
    CONTROL_DB.parent.mkdir(parents=True,exist_ok=True)
    disk=shutil.disk_usage(CONTROL_DB.parent); free_percent=round(disk.free/disk.total*100,1)
    ready=latency<1000 and free_percent>=5 and run is not None
    return {"status":"ready" if ready else "degraded","service":"smtcs-static-test-control",
            "database":{"status":"ready","latency_ms":latency,"journal_mode":"WAL"},
            "disk":{"free_bytes":disk.free,"free_percent":free_percent,"status":"ready" if free_percent>=5 else "critical"},
            "operation":dict(operation) if operation else None,"active_run":dict(run) if run else None,
            "recording":recording,"edge":dict(edge) if edge else {"status":"NO_DEVICE"},
            "security":{"development_secret":app.config["SECRET_KEY"]=="development-only-change-me"}}, (200 if ready else 503)


@app.get("/health/live")
def liveness():
    return {"status":"alive","service":"smtcs-static-test-control"}


if __name__ == "__main__":
    init_control_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")), debug=os.environ.get("FLASK_DEBUG") == "1")
