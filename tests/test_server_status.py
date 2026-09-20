"""Offline tests for the Home Assistant-facing endpoints in webapp/server.py.

Needs fastapi's TestClient (httpx); skipped if it isn't installed.
Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_display_data import encode_line, reg_block

from cts600 import display_data, state as state_mod

try:
    from fastapi.testclient import TestClient

    from cts600.webapp import server
except (ImportError, RuntimeError):  # starlette raises RuntimeError without httpx
    TestClient = None


class FakeListener:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def send_key(self, key, hold_seconds=0.3, use_rts=True):
        self.keys.append(key)

    def _bus_healthy(self):
        return True


@unittest.skipIf(TestClient is None, "fastapi TestClient not available")
class StatusEndpointTests(unittest.TestCase):
    def setUp(self):
        self.state = state_mod.DeviceState(readings_path=None)
        for reg, text in ((display_data.LINE1_REG, "LÄMPÖ"), (display_data.LINE2_REG, ">2< 21°C")) * 2:
            self.state.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))

    def test_status_passive(self):
        client = TestClient(server.create_app(self.state))
        body = client.get("/api/status").json()
        self.assertFalse(body["control_enabled"])
        self.assertEqual(body["panel"]["mode"], "heat")
        self.assertEqual(body["panel"]["setpoint"], 21)
        r = client.post("/api/settings", json={"setpoint": 22})
        self.assertEqual(r.status_code, 503)

    def test_settings_validation(self):
        client = TestClient(server.create_app(self.state, listener=FakeListener()))
        self.assertEqual(client.post("/api/settings", json={"setpoint": 99}).status_code, 400)
        self.assertEqual(client.post("/api/settings", json={}).status_code, 400)

    def test_ws_status_pushes_changes(self):
        client = TestClient(server.create_app(self.state))
        with client.websocket_connect("/ws/status") as ws:
            first = ws.receive_json()
            self.assertEqual(first["panel"]["setpoint"], 21)
            for reg, text in ((display_data.LINE2_REG, ">2< 23°C"), (display_data.LINE1_REG, "LÄMPÖ"),
                              (display_data.LINE2_REG, ">2< 23°C")):
                self.state.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))
            self.assertEqual(ws.receive_json()["panel"]["setpoint"], 23)


if __name__ == "__main__":
    unittest.main()
