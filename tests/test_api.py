from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from hermes_soul.api import create_app


class APITests(unittest.TestCase):
    def test_auth_and_pause_resume_and_interest_provenance(self):
        with tempfile.TemporaryDirectory() as home:
            token = Path(home) / "token"
            token.write_text("x" * 32)
            cfg = {"home": home, "access_token_file": str(token), "mcp_url": "http://127.0.0.1:1/mcp",
                   "interval_seconds": 900, "idle_max_seconds": 3600, "max_calls_per_day": 2}
            with patch("hermes_soul.runtime.Runtime.loop", new=AsyncMock()):
                with TestClient(create_app(cfg)) as client:
                    self.assertEqual(client.get("/").status_code, 200)
                    self.assertEqual(client.get("/api/state").status_code, 401)
                    client.headers["Authorization"] = "Bearer " + "x" * 32
                    self.assertEqual(client.post("/api/control/pause").status_code, 200)
                    self.assertEqual(client.post("/api/control/wake").status_code, 409)
                    self.assertEqual(client.post("/api/interests", json={"topic": "猫"}).status_code, 200)
                    state = client.get("/api/state").json()
                    record = state["records"][0]
                    self.assertEqual(record["payload"]["state"], "suggested")
                    self.assertTrue(record["refs"])
                    self.assertEqual(client.post("/api/control/resume").status_code, 200)


if __name__ == "__main__":
    unittest.main()
