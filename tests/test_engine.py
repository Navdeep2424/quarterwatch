import json
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quarterwatch as g


class TrialTests(unittest.TestCase):
    def test_bet365_overtime_period(self):
        rows = [{"type": "EV", "CP": "OT 1", "TM": "2", "TS": "14", "SS": "56-58"}]
        self.assertEqual(g.event_snapshot(rows), (5, 134, 56, 58))
        self.assertEqual(g.period("OT 2"), 6)

    def test_bet365_basketball_payload_shape(self):
        sample = json.loads((Path(__file__).parent / "fixtures/basketball.json").read_text())
        rows = g.records(sample)
        self.assertEqual(g.event_snapshot(rows), (1, 406, 4, 5))
        self.assertEqual(g.full_game_under(rows), (158.5, "5/6", "Total 2-Way"))

    def test_q4_decision_and_settlement(self):
        with tempfile.TemporaryDirectory() as folder:
            old_path = g.DB_PATH
            g.DB_PATH = Path(folder) / "test.sqlite3"
            try:
                g.initialize()
                g.record_event("7", "A vs B", 4, 200, 90, 88)
                points, margin = g.record_observation("7", 4, 200, 90, 88, 199.5, "5/6")
                self.assertEqual((points, margin), (21.5, 2))
                label, reason = g.classify(points, 200, margin)
                self.assertEqual(label, "PLAY_UNDER")
                g.record_decision("7", label, reason, 200, 90, 88, 199.5, "5/6", points, margin)
                g.record_decision("7", "SKIP", "duplicate", 200, 90, 88, 199.5, "5/6", points, margin)
                g.settle("7", 96, 99)
                with g.connect() as db:
                    row = db.execute("SELECT * FROM decisions WHERE event_id='7'").fetchone()
                    self.assertEqual(row["decision"], "PLAY_UNDER")
                    self.assertEqual(row["outcome"], "win")
                    self.assertAlmostEqual(row["pnl_units"], 5 / 6)
                    self.assertEqual(db.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
            finally:
                g.DB_PATH = old_path

    def test_no_quarter_or_team_total(self):
        rows = [
            {"type": "MA", "NA": "4th Quarter Total"},
            {"type": "PA", "NA": "Under", "HA": "44.5", "OD": "5/6"},
            {"type": "MA", "NA": "Team Total"},
            {"type": "PA", "NA": "Under", "HA": "80.5", "OD": "5/6"},
            {"type": "MA", "NA": "Total 2-Way"},
            {"type": "PA", "NA": "Under", "HA": "199.5", "OD": "5/6"},
        ]
        self.assertEqual(g.full_game_under(rows)[0], 199.5)

    def test_live_league_name_matching(self):
        payload = {
            "results": [
                {"id": "1", "league": {"id": "10068518", "name": "Ebasketball H2H GG League - 4x5mins"}},
                {"id": "2", "league": {"id": "999", "name": "Other Basketball"}},
            ]
        }
        self.assertEqual(list(g.filter_events(payload)), ["1"])

    def test_live_gg_market_shape(self):
        rows = [
            {"type": "MG", "NA": "Game Lines"},
            {"type": "MA", "NA": "Total", "ID": "1450"},
            {"type": "PA", "HA": "110.5", "HD": "O 110.5", "OD": "5/6"},
            {"type": "PA", "HA": "110.5", "HD": "U 110.5", "OD": "5/6"},
            {"type": "MG", "NA": "4th Quarter Lines"},
            {"type": "MA", "NA": "Total", "ID": "180107"},
            {"type": "PA", "HA": "29.5", "HD": "U 29.5", "OD": "5/6"},
        ]
        self.assertEqual(g.full_game_under(rows), (110.5, "5/6", "Total"))

    def test_result_joins_on_bet365_id(self):
        self.assertEqual(g.result_event_id({"id": "internal-42", "bet365_id": "fixture-42"}), "fixture-42")

    def test_suspended_parent_market_is_not_used(self):
        rows = [
            {"type": "MG", "NA": "Game Lines", "SU": "1"},
            {"type": "MA", "NA": "Total", "SU": "0"},
            {"type": "PA", "HD": "U 110.5", "HA": "110.5", "OD": "5/6", "SU": "0"},
        ]
        self.assertEqual(g.full_game_under(rows), (None, None, None))

    def test_window_polling_replays_clock_jumps_and_keeps_polling_after_pick(self):
        timestamp = 10000.0
        detail_clocks = {event_id: [] for event_id in ("7", "8", "9", "10")}
        detail_times = []

        def sleep(delay):
            nonlocal timestamp
            timestamp += delay

        def event(event_id, seconds):
            return {"type": "EV", "FI": event_id, "CP": "Q4", "SS": "90-88", "time_remaining": seconds}

        def fetch(endpoint, params=None):
            if endpoint == "inplay_filter":
                return {
                    "results": [
                        {"id": event_id, "league": {"name": "GG League"}} for event_id in detail_clocks
                    ]
                }
            if endpoint == "inplay":
                return {"results": [event("7", 207), event("8", 210), event("9", 191), event("10", 205)]}
            self.assertEqual(endpoint, "event")
            event_id = params["FI"]
            if event_id == "7":
                detail_times.append(timestamp)
            elapsed = int(timestamp - detail_times[0]) if detail_times else 0
            clock = {"7": 207 - elapsed, "8": 210 if elapsed < 2 else 190, "10": 205 - elapsed}[event_id]
            detail_clocks[event_id].append(clock)
            rows = [event(event_id, clock)]
            if event_id != "10":
                rows.extend(
                    [
                        {"type": "MA", "NA": "Total 2-Way"},
                        {"type": "PA", "NA": "Under", "HA": "199.5", "OD": "5/6"},
                    ]
                )
            return {"results": rows}

        class ImmediatePool:
            def __init__(self, max_workers):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, call, *args):
                future = Future()
                future.set_result(call(*args))
                return future

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.multiple(
                g,
                DB_PATH=Path(folder) / "window.sqlite3",
                now=lambda: timestamp,
                TRIAL_SECONDS=20,
                ThreadPoolExecutor=ImmediatePool,
            ),
            patch.object(g.time, "sleep", sleep),
            patch.object(g.ApiClient, "get", side_effect=fetch),
        ):
            g.initialize()
            g.engine("fixture-token")
            decisions = {row["event_id"]: row for row in g.state()["decisions"]}

        self.assertEqual(detail_clocks["7"][:2], [207, 206])
        self.assertGreaterEqual(len(detail_times), 13)
        self.assertTrue(all(0.99 <= b - a <= 1.11 for a, b in zip(detail_times, detail_times[1:])))
        self.assertLess(detail_clocks["7"][-1], g.WINDOW_CLOSE)
        self.assertGreaterEqual(detail_clocks["7"][-2], g.WINDOW_CLOSE)
        self.assertEqual((decisions["7"]["decision"], decisions["7"]["seconds_left"]), ("PLAY_UNDER", 206))
        self.assertEqual(detail_clocks["8"], [210, 210, 190])
        self.assertEqual(detail_clocks["9"], [])
        for event_id in ("8", "9", "10"):
            self.assertEqual(decisions[event_id]["decision"], "MISSED")
            self.assertIsNone(decisions[event_id]["total_line"])
        self.assertEqual(decisions["8"]["reason"], g.MISSED_CLOCK)
        self.assertEqual(decisions["10"]["reason"], g.MISSED_MARKET)


if __name__ == "__main__":
    unittest.main()
