from __future__ import annotations

import json
from pathlib import Path

from .database import connect_database


def ensure_pressure_edge_integration(db_path: Path, operation_id: str) -> dict:
    """Configure PT-01 for inbound SMTCS-EDGE/1 telemetry.

    The ESP32 sends integer pressure_mbar samples over Ethernet. Converting mbar
    to bar in the channel mapping keeps the wire format deterministic and leaves
    engineering calibration visible/auditable in Stellar Ops.
    """
    device_id = "PT-01"
    channel_id = "motor.chamber_pressure"

    db = connect_database(db_path)
    try:
        device = db.execute(
            "SELECT 1 FROM devices WHERE operation_id=? AND id=?",
            (operation_id, device_id),
        ).fetchone()
        if not device:
            raise RuntimeError(f"Configured pressure device {device_id} does not exist in Device Registry")

        db.execute(
            """UPDATE devices
               SET protocol='SMTCS-EDGE/1', endpoint='INBOUND TCP :9100'
               WHERE operation_id=? AND id=?""",
            (operation_id, device_id),
        )
        db.execute(
            """INSERT INTO device_integrations(
                 operation_id,device_id,adapter_type,config_json,enabled,
                 last_test_at,last_test_status,last_test_message)
               VALUES(?,?, 'SMTCS_EDGE_TCP', ?,1,NULL,'WAITING_FOR_DEVICE',NULL)
               ON CONFLICT(operation_id,device_id) DO UPDATE SET
                 adapter_type='SMTCS_EDGE_TCP', config_json=excluded.config_json, enabled=1""",
            (
                operation_id,
                device_id,
                json.dumps(
                    {
                        "transport": "SMTCS_EDGE_TCP",
                        "direction": "INBOUND",
                        "listen_port": 9100,
                        "device_id": device_id,
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        db.execute(
            """UPDATE channels
               SET source_id=?, quality='NO_DATA', sample_rate=200
               WHERE operation_id=? AND id=?""",
            (device_id, operation_id, channel_id),
        )
        db.execute(
            """UPDATE channel_integrations
               SET raw_field='pressure_mbar',
                   calibration_slope=0.001,
                   calibration_intercept=0,
                   stale_timeout_ms=500
               WHERE operation_id=? AND channel_id=?""",
            (operation_id, channel_id),
        )
        db.commit()
    finally:
        db.close()

    return {
        "device_id": device_id,
        "channel_id": channel_id,
        "transport": "SMTCS_EDGE_TCP",
        "listen_port": 9100,
        "wire_field": "pressure_mbar",
        "engineering_unit": "bar",
        "sample_rate_hz": 200,
    }
