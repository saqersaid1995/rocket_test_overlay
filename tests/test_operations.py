import tempfile
import unittest
from pathlib import Path

import operations
from app import app


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = operations.OPERATIONS_DB
        operations.OPERATIONS_DB = Path(self.temp_dir.name) / "operations.db"
        operations.init_operations_db()
        app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = app.test_client()

    def tearDown(self):
        operations.OPERATIONS_DB = self.original_db
        self.temp_dir.cleanup()

    def test_studio_route_remains_available(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ROCKET OVERLAY", response.data)

    def test_operations_dashboard_is_isolated(self):
        response = self.client.get("/operations/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Command Center", response.data)
        self.assertIn(b"Open Overlay Studio", response.data)

    def test_program_lifecycle(self):
        created = self.client.post(
            "/operations/programs/new",
            data={
                "code": "LV-QUAL",
                "name": "QualSRM Flight Demonstration",
                "program_type": "Flight Demonstration",
                "status": "Planning",
                "owner": "Program Team",
                "objective": "Qualify the vehicle through a controlled flight.",
                "start_date": "2026-08-01",
                "target_date": "2026-10-01",
            },
            follow_redirects=True,
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn(b"QualSRM Flight Demonstration", created.data)

        listing = self.client.get("/operations/programs")
        self.assertIn(b"LV-QUAL", listing.data)

        with operations._db() as connection:
            program = connection.execute(
                "SELECT * FROM programs WHERE code = 'LV-QUAL'"
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM operation_events WHERE entity_id = ?",
                (program["id"],),
            ).fetchone()[0]
        self.assertEqual(program["status"], "Planning")
        self.assertEqual(event_count, 1)

        archived = self.client.post(
            f"/operations/programs/{program['id']}/archive",
            follow_redirects=True,
        )
        self.assertEqual(archived.status_code, 200)
        archived_listing = self.client.get(
            "/operations/programs?status=Archived"
        )
        self.assertIn(b"LV-QUAL", archived_listing.data)

    def test_duplicate_code_and_invalid_dates_are_rejected(self):
        payload = {
            "code": "PROP-01",
            "name": "Propulsion Development",
            "program_type": "Propulsion",
            "status": "Active",
            "owner": "Propulsion",
            "objective": "",
            "start_date": "2026-09-01",
            "target_date": "2026-08-01",
        }
        response = self.client.post("/operations/programs/new", data=payload)
        self.assertIn(b"Target date cannot be earlier", response.data)


if __name__ == "__main__":
    unittest.main()
