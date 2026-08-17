from __future__ import annotations

import argparse
import json
import os
import socketserver
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .edge_protocol import ProtocolError, decode_frame, encode_frame

DEFAULT_DB = Path(os.environ.get("STELLAR_OPS_DATA", Path(__file__).resolve().parent / "data")) / "control.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS edge_sessions(
      device_id TEXT NOT NULL, boot_id TEXT NOT NULL, remote_addr TEXT NOT NULL,
      firmware TEXT, connected_at TEXT NOT NULL, disconnected_at TEXT,
      last_seen TEXT NOT NULL, last_sequence INTEGER, total_samples INTEGER NOT NULL DEFAULT 0,
      sequence_gaps INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
      PRIMARY KEY(device_id,boot_id));
    CREATE TABLE IF NOT EXISTS edge_batches(
      id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, boot_id TEXT NOT NULL,
      sequence INTEGER NOT NULL, received_at TEXT NOT NULL, first_sample_us INTEGER NOT NULL,
      sample_period_us INTEGER NOT NULL, sample_count INTEGER NOT NULL,
      channels_json TEXT NOT NULL, UNIQUE(device_id,boot_id,sequence));
    """)
    return db


class EdgeHandler(socketserver.StreamRequestHandler):
    device_id: str | None = None
    boot_id: str | None = None

    def reply(self, payload: dict) -> None:
        self.wfile.write(encode_frame({"protocol": "SMTCS-EDGE/1", "device_id": self.device_id or "GATEWAY",
                                      "boot_id": self.boot_id or "GATEWAY", **payload}))

    def handle(self) -> None:
        db = database(self.server.db_path)
        remote = f"{self.client_address[0]}:{self.client_address[1]}"
        try:
            for frame in self.rfile:
                try:
                    msg = decode_frame(frame)
                    self.device_id, self.boot_id = msg["device_id"], msg["boot_id"]
                    if msg["type"] == "HELLO":
                        stamp = now()
                        db.execute("""INSERT INTO edge_sessions(device_id,boot_id,remote_addr,firmware,connected_at,last_seen,status)
                          VALUES(?,?,?,?,?,?, 'CONNECTED') ON CONFLICT(device_id,boot_id) DO UPDATE SET
                          remote_addr=excluded.remote_addr,firmware=excluded.firmware,last_seen=excluded.last_seen,
                          disconnected_at=NULL,status='CONNECTED'""",
                          (self.device_id,self.boot_id,remote,msg.get("firmware"),stamp,stamp)); db.commit()
                        self.reply({"type":"ACK","ack_sequence":-1,"gateway_time_utc":stamp})
                    elif msg["type"] == "HEARTBEAT":
                        db.execute("UPDATE edge_sessions SET last_seen=?,status='CONNECTED' WHERE device_id=? AND boot_id=?",
                                   (now(),self.device_id,self.boot_id)); db.commit()
                        self.reply({"type":"ACK","ack_sequence":msg.get("sequence",-1),"gateway_time_utc":now()})
                    else:
                        session=db.execute("SELECT last_sequence FROM edge_sessions WHERE device_id=? AND boot_id=?",
                                           (self.device_id,self.boot_id)).fetchone()
                        if session is None: raise ProtocolError("HELLO required before BATCH")
                        last=session["last_sequence"]; gap=max(0,msg["sequence"]-(last+1)) if last is not None else 0
                        db.execute("""INSERT OR IGNORE INTO edge_batches(device_id,boot_id,sequence,received_at,first_sample_us,sample_period_us,sample_count,channels_json)
                          VALUES(?,?,?,?,?,?,?,?)""",(self.device_id,self.boot_id,msg["sequence"],now(),msg["first_sample_us"],msg["sample_period_us"],msg["sample_count"],json.dumps(msg["channels"],separators=(",",":"))))
                        db.execute("""UPDATE edge_sessions SET last_seen=?,last_sequence=?,total_samples=total_samples+?,sequence_gaps=sequence_gaps+?,status='STREAMING'
                          WHERE device_id=? AND boot_id=?""",(now(),msg["sequence"],msg["sample_count"],gap,self.device_id,self.boot_id)); db.commit()
                        self.reply({"type":"ACK","ack_sequence":msg["sequence"],"gateway_time_utc":now()})
                except ProtocolError as exc:
                    self.reply({"type":"NACK","reason":str(exc),"gateway_time_utc":now()})
        finally:
            if self.device_id and self.boot_id:
                db.execute("UPDATE edge_sessions SET disconnected_at=?,last_seen=?,status='DISCONNECTED' WHERE device_id=? AND boot_id=?",
                           (now(),now(),self.device_id,self.boot_id)); db.commit()
            db.close()


class Gateway(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self,address,db_path): self.db_path=db_path; super().__init__(address,EdgeHandler)


def main() -> None:
    parser=argparse.ArgumentParser(description="SMTCS Ethernet telemetry gateway")
    parser.add_argument("--host",default="0.0.0.0"); parser.add_argument("--port",type=int,default=9100)
    parser.add_argument("--database",type=Path,default=DEFAULT_DB); args=parser.parse_args()
    with Gateway((args.host,args.port),args.database) as server:
        print(f"SMTCS Edge Gateway listening on {args.host}:{args.port}",flush=True); server.serve_forever()


if __name__ == "__main__": main()

