import sqlite3
import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module
from stellar_ops.audit_integrity import verify_audit_ledger


class AuditIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.client.get("/ops")

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_events_commands_and_incidents_are_hash_chained(self):
        command = self.client.post(
            "/api/control/command",
            json={
                "action": "HOLD",
                "reason": "Integrity test",
                "command_id": "audit-command-001",
            },
        )
        self.assertEqual(command.status_code, 200)
        incident = self.client.post(
            "/api/control/incident",
            json={
                "severity": "P2",
                "category": "SAFETY",
                "title": "Audit verification incident",
                "description": "Verify controlled incident records enter the ledger.",
                "owner": "TEST DIRECTOR",
            },
        )
        self.assertEqual(incident.status_code, 201)

        status = self.client.get("/api/control/integrity")
        self.assertEqual(status.status_code, 200)
        result = status.get_json()["integrity"]
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["head_hash"]), 64)

        with control_module.connect() as db:
            record_types = {
                row["record_type"]
                for row in db.execute("SELECT record_type FROM audit_ledger")
            }
        self.assertIn("EVENT", record_types)
        self.assertIn("COMMAND", record_types)
        self.assertIn("INCIDENT_ACTION", record_types)

    def test_operational_audit_tables_are_database_protected(self):
        self.client.post(
            "/api/control/command",
            json={
                "action": "HOLD",
                "reason": "Trigger test",
                "command_id": "protected-command-001",
            },
        )
        with control_module.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """UPDATE command_journal SET outcome='ALTERED'
                       WHERE command_id='protected-command-001'"""
                )
        with control_module.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")

    def test_hash_tampering_is_detected_and_degrades_health(self):
        self.client.post(
            "/api/control/command",
            json={
                "action": "HOLD",
                "reason": "Tamper test",
                "command_id": "tamper-command-001",
            },
        )
        with control_module.connect() as db:
            db.execute("DROP TRIGGER protect_audit_ledger_update")
            db.execute(
                """UPDATE audit_ledger SET entry_hash=?
                   WHERE sequence=(SELECT min(sequence) FROM audit_ledger)""",
                ("0" * 64,),
            )
            result = verify_audit_ledger(db)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "FAILED")

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 503)
        self.assertFalse(health.get_json()["audit_integrity"]["valid"])


if __name__ == "__main__":
    unittest.main()
