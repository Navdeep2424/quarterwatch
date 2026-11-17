import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import quarterwatch as app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(app, "DB_PATH", Path(self.temp.name) / "trial.sqlite3")
        self.paths.start()
        app.initialize()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_concurrent_requests_cannot_exceed_rolling_budget(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"success": 1, "results": []}
        with patch.object(app.requests, "Session") as session, patch.object(app, "HOURLY_BUDGET", 2):
            session.return_value.get.return_value = response
            client = app.ApiClient("synthetic-secret")
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: client.get("inplay"), range(6)))
            self.assertEqual(sum(result is not None for result in results), 2)
            self.assertEqual(session.return_value.get.call_count, 2)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM calls").fetchone()[0], 2)

    def test_provider_reset_header_pauses_requests(self):
        response = Mock(
            status_code=200,
            headers={
                "X-RateLimit-Limit": "1800",
                "X-RateLimit-Remaining": "10",
                "X-RateLimit-Reset": "20000",
            },
        )
        response.json.return_value = {"success": 1, "results": []}
        with patch.object(app, "now", return_value=10000), patch.object(app.requests, "Session") as session:
            session.return_value.get.return_value = response
            client = app.ApiClient("synthetic-secret")
            self.assertIsNotNone(client.get("inplay"))
            self.assertIsNone(client.get("inplay"))
            self.assertEqual(client.blocked_until, 20002)
            self.assertEqual(session.return_value.get.call_count, 1)

    def test_timeout_is_logged_without_token_bearing_url(self):
        with patch.object(app.requests, "Session") as session:
            session.return_value.get.side_effect = requests.Timeout("URL?token=synthetic-secret")
            with self.assertLogs(app.logger, level="INFO") as log:
                self.assertIsNone(app.ApiClient("synthetic-secret").get("event", {"FI": "42"}))
        self.assertNotIn("synthetic-secret", "\n".join(log.output))
        self.assertNotIn("synthetic-secret", app.meta_get("last_error"))
        with app.connect() as db:
            row = db.execute("SELECT ok,http_status FROM calls").fetchone()
            self.assertEqual(tuple(row), (0, None))
