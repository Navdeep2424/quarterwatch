"""Deterministic fictional games for evaluating the dashboard without API access."""

import time


def seed_demo(app):
    """Exercise real observation, decision, and settlement paths with synthetic data."""
    examples = [
        ("demo-01", "Boston Comets · Brooklyn Kings", 199.5, 90, 88, 196),
        ("demo-02", "Toronto Huskies · Chicago Storm", 189.5, 86, 83, 192),
        ("demo-03", "Denver Peaks · Phoenix Fire", 181.5, 82, 80, 178),
        ("demo-04", "Miami Waves · Seattle Sound", 209.5, 100, 90, 206),
        ("demo-05", "Portland Pines · Atlanta Flight", 193.5, 87, 87, 190),
        ("demo-06", "Dallas Orbit · Memphis Blues", 184.5, 84, 82, 188),
        ("demo-07", "Orlando Tide · Cleveland Forge", 201.0, 92, 89, 201),
    ]
    for event_id, name, line, home, away, final_total in examples:
        if app.decision_exists(event_id):
            continue
        app.record_event(event_id, name, 4, 206, home, away)
        points, margin = app.record_observation(event_id, 4, 206, home, away, line, "5/6")
        label, reason = app.classify(points, 206, margin)
        app.record_decision(event_id, label, reason, 206, home, away, line, "5/6", points, margin)
        app.settle(event_id, final_total // 2, final_total - final_total // 2)
    if not app.decision_exists("demo-gap"):
        app.record_event("demo-gap", "Detroit Motors · Milwaukee Oaks", 4, 208, 88, 90)
        app.record_observation("demo-gap", 4, 208, 88, 90, 198.5, "5/6")
        app.record_observation("demo-gap", 4, 192, 90, 90, 198.5, "5/6")
        app.record_missed_window("demo-gap", 192, 90, 90)
        app.settle("demo-gap", 98, 99)


def run_demo(app):
    seed_demo(app)
    app.meta_set("trial_start", app.now())
    app.meta_set("status", "running")
    app.meta_set("games_seen", 3)
    cycle = 0
    while True:
        elapsed = 0
        watch = app.QuarterWatch("Toronto Huskies · Boston Comets", 224, app.now())
        event_id = f"replay-{cycle}"
        while elapsed <= 38:
            clock = 224 - elapsed
            rows = [
                {"type": "EV", "CP": "Q4", "time_remaining": clock, "SS": "87-89"},
                {"type": "MG", "NA": "Game Lines"},
                {"type": "MA", "NA": "Total"},
                {"type": "PA", "HD": "U 195.5", "HA": "195.5", "OD": "5/6"},
            ]
            if clock >= app.WINDOW_CLOSE or elapsed == 32:
                app.apply_detail(event_id, watch, {"results": [rows]})
            app.record_event("demo-q3", "Los Angeles Stars · Denver Peaks", 3, 188 - elapsed, 54, 56)
            app.record_event("demo-ot", "Miami Waves · Phoenix Fire", 5, 142 - elapsed, 104, 104)
            app.meta_set("last_success", app.now())
            time.sleep(2)
            elapsed += 2
        app.settle(event_id, 96, 98 if cycle % 2 == 0 else 101)
        cycle += 1
