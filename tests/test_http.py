import csv
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import quarterwatch as app
from demo import seed_demo


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(app, "DB_PATH", Path(self.temp.name) / "trial.sqlite3")
        self.paths.start()
        app.initialize()
        seed_demo(app)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.paths.stop()
        self.temp.cleanup()

    def read(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.read().decode()

    def test_dashboard_and_exports_through_real_http_entry_points(self):
        self.assertIn("QuarterWatch", self.read("/"))
        self.assertIn("function render", self.read("/app.js"))
        self.assertIn(".sidebar", self.read("/styles.css"))
        data = json.loads(self.read("/api/state"))
        self.assertEqual(data["totals"]["picks"], 5)
        self.assertEqual(data["totals"]["wins"], 3)
        self.assertEqual(data["totals"]["losses"], 1)
        self.assertEqual(data["totals"]["pushes"], 1)
        rows = list(csv.DictReader(io.StringIO(self.read("/export/decisions.csv"))))
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["line_result"] for row in rows}, {"hit", "miss", "push", "unavailable"})
        self.assertEqual(rows[0]["under_odds"], "1.83")
        self.assertEqual(data["calls_total"], 0)
        # Seeding a replay twice must not duplicate paper picks or observations.
        seed_demo(app)
        self.assertEqual(json.loads(self.read("/api/state"))["totals"]["decisions"], 8)
