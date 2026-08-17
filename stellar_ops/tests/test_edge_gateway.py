import socket
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from stellar_ops.edge_gateway import Gateway, database
from stellar_ops.edge_protocol import ProtocolError, batch, decode_frame, encode_frame, hello


class EdgeProtocolTests(unittest.TestCase):
    def test_crc_and_batch_validation(self):
        message = batch("ESP-DAQ-01", "boot-1", 0, 0, 1000, {"pressure_raw": [1.0, 2.0]})
        decoded = decode_frame(encode_frame(message))
        self.assertEqual(decoded["sample_count"], 2)
        corrupt = bytearray(encode_frame(message)); corrupt[20] ^= 1
        with self.assertRaises(ProtocolError):
            decode_frame(bytes(corrupt))

    def test_gateway_persists_acknowledged_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.db"
            server = Gateway(("127.0.0.1", 0), path)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            boot = str(uuid.uuid4())
            try:
                with socket.create_connection(server.server_address, timeout=2) as sock:
                    stream = sock.makefile("rwb")
                    stream.write(encode_frame(hello("ESP-DAQ-01", boot, "test-1.0", ["pressure_raw"]))); stream.flush()
                    self.assertEqual(decode_frame(stream.readline())["type"], "ACK")
                    stream.write(encode_frame(batch("ESP-DAQ-01", boot, 0, 0, 1000, {"pressure_raw": [1.0, 2.0, 3.0]}))); stream.flush()
                    ack = decode_frame(stream.readline())
                    self.assertEqual(ack["ack_sequence"], 0)
                with database(path) as db:
                    session = db.execute("SELECT * FROM edge_sessions WHERE device_id='ESP-DAQ-01'").fetchone()
                    stored = db.execute("SELECT * FROM edge_batches").fetchone()
                self.assertEqual(session["total_samples"], 3)
                self.assertEqual(stored["sample_count"], 3)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
