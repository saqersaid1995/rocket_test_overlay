import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module
from stellar_ops.recovery import restore_backup


class BackupRecoveryTests(unittest.TestCase):
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

    def create_verified_backup(self):
        response = self.client.post(
            "/api/control/backups",
            json={
                "reason": "Pre-operation recovery checkpoint",
                "actor": "TEST DIRECTOR",
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()["backup"]

    def test_backup_is_created_cataloged_and_verified(self):
        backup = self.create_verified_backup()
        self.assertEqual(backup["state"], "VERIFIED")
        self.assertEqual(len(backup["sha256"]), 64)
        self.assertEqual(len(backup["audit_head_hash"]), 64)

        catalog = self.client.get("/api/control/backups").get_json()["backups"]
        self.assertEqual(catalog[0]["backup_name"], backup["backup_name"])

        verified = self.client.post(
            f"/api/control/backups/{backup['backup_name']}/verify"
        )
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.get_json()["verification"]["valid"])

    def test_backup_is_blocked_during_transient_execution(self):
        with control_module.connect() as db:
            db.execute(
                "UPDATE operations SET state='COUNTDOWN' WHERE id=?",
                (control_module.OPERATION_ID,),
            )
        response = self.client.post(
            "/api/control/backups",
            json={"reason": "Unsafe online backup attempt"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("CHECKOUT or HOLD", response.get_json()["error"])

    def test_offline_restore_requires_exact_confirmation_and_keeps_rollback(self):
        backup = self.create_verified_backup()
        with self.assertRaises(ValueError):
            restore_backup(
                database_path=control_module.CONTROL_DB,
                backup_name=backup["backup_name"],
                confirmation="RESTORE",
            )

        self.client.post(
            "/api/control/command",
            json={
                "action": "HOLD",
                "reason": "Change after checkpoint",
                "command_id": "after-backup-command",
            },
        )
        restored = restore_backup(
            database_path=control_module.CONTROL_DB,
            backup_name=backup["backup_name"],
            confirmation=f"RESTORE {backup['backup_name']}",
        )
        self.assertTrue(restored["restored"])
        self.assertTrue(Path(restored["rollback_path"]).is_file())

        with control_module.connect() as db:
            changed_command = db.execute(
                """SELECT 1 FROM command_journal
                   WHERE command_id='after-backup-command'"""
            ).fetchone()
        self.assertIsNone(changed_command)

    def test_checksum_tampering_refuses_backup(self):
        backup = self.create_verified_backup()
        path = (
            control_module.CONTROL_DB.parent
            / "backups"
            / backup["backup_name"]
        )
        with path.open("ab") as handle:
            handle.write(b"TAMPER")
        response = self.client.post(
            f"/api/control/backups/{backup['backup_name']}/verify"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["verification"]["status"],
            "CHECKSUM_FAILED",
        )


if __name__ == "__main__":
    unittest.main()
