#!/usr/bin/env python3
"""QuarterWatch: quota-aware live sports monitoring and paper-trial analysis."""

import argparse
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_URL = "https://api.b365api.com/v1/bet365"
GG_LEAGUE_IDS = {"25067", "10068518"}  # legacy script ID and live Bet365 filter ID
SPORT_ID = "18"  # BetsAPI basketball
TRIAL_SECONDS = 24 * 60 * 60
DISCOVERY_SECONDS = 45
HOURLY_BUDGET = 1600  # headroom below a 1,800-call provider allowance
TARGET_SECONDS = 200
TOLERANCE_SECONDS = 6
WINDOW_OPEN = TARGET_SECONDS + TOLERANCE_SECONDS
WINDOW_CLOSE = TARGET_SECONDS - TOLERANCE_SECONDS
FAST_POLL_START = WINDOW_OPEN + 8
FAST_POLL_SECONDS = 1.5
WINDOW_POLL_START = WINDOW_OPEN + 4
WINDOW_POLL_SECONDS = 1.0
MAX_DETAIL_REQUESTS = 8
MISSED_CLOCK = "No Q4 clock sample from 3:26 to 3:14"
MISSED_MARKET = "No usable score and total from 3:26 to 3:14"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "trial.sqlite3"
HTML_PATH = ROOT / "web" / "index.html"
logger = logging.getLogger(__name__)


def now():
    return time.time()


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def score(value):
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def period(value):
    text = str(value or "").strip().lower()
    overtime = re.fullmatch(r"(?:ot|overtime)\s*([1-9]\d*)?", text)
    if overtime:
        return 4 + int(overtime.group(1) or 1)
    if re.search(r"(?:^|\b)(?:q4|4th quarter|fourth quarter)(?:\b|$)", text):
        return 4
    match = re.fullmatch(r"q?([1-4])", text)
    return int(match.group(1)) if match else None


def remaining_seconds(obj):
    direct = integer(obj.get("time_remaining"))
    if direct is not None:
        return direct
    minute, second = integer(obj.get("TM")), integer(obj.get("TS"))
    if minute is not None and second is not None and 0 <= second < 60:
        return minute * 60 + second
    return None


def records(payload):
    """Flatten documented Bet365 results: [[{type: EV}, {type: MA}, ...]]."""
    result = payload.get("results", []) if isinstance(payload, dict) else payload
    if isinstance(result, dict):
        return [result]
    if not isinstance(result, list):
        return []
    out = []
    for item in result:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            out.extend(row for row in item if isinstance(row, dict))
    return out


def event_id_from_ev(row):
    if row.get("FI"):
        return str(row["FI"])
    # Bet365 inplay EV IDs look like 92835527C18A_1_1.
    match = re.match(r"^(\d+)C", str(row.get("ID", "")))
    return match.group(1) if match else None


def result_event_id(row):
    # /bet365/result uses a different internal `id`; `bet365_id` is our live ID.
    return str(row.get("bet365_id") or row.get("id") or row.get("event_id") or "")


def inplay_events(payload):
    found = {}
    for row in records(payload):
        if row.get("type") != "EV":
            continue
        event_id = event_id_from_ev(row)
        if event_id:
            found[event_id] = row
    return found


def filter_events(payload):
    return {
        str(row["id"]): row
        for row in records(payload)
        if row.get("id")
        and (
            "gg league" in str((row.get("league") or {}).get("name", "")).lower()
            or str((row.get("league") or {}).get("id")) in GG_LEAGUE_IDS
        )
    }


def full_game_under(rows):
    """Select the active full-game Under from a Total market, excluding Q/half/team totals."""
    market = None
    group = ""
    group_suspended = False
    for row in rows:
        typ = row.get("type")
        if typ == "MG":
            if str(row.get("NA") or "").strip():
                group = str(row["NA"]).strip().lower()
                group_suspended = str(row.get("SU", "0")) == "1"
            market = None
        elif typ == "MA":
            name = str(row.get("NA", "")).strip().lower()
            full_game_group = group in ("game lines", "main markets", "game totals", "")
            valid = (
                ("total" in name or "over/under" in name)
                and full_game_group
                and not any(
                    bad in name
                    for bad in ("quarter", "half", "team", "player", "1st", "2nd", "3rd", "4th", "period")
                )
            )
            market = row if valid and not group_suspended and str(row.get("SU", "0")) != "1" else None
        elif typ == "PA" and market and str(row.get("SU", "0")) != "1":
            under = (
                str(row.get("NA", "")).strip().lower() == "under"
                or row.get("IR") == "U"
                or bool(re.match(r"^\s*(?:U|Under)\s+\d", str(row.get("HD") or ""), re.I))
            )
            if under:
                line = number(row.get("HA"))
                if line is not None and line > 0:
                    return line, str(row.get("OD", "")), str(market.get("NA", ""))
    return None, None, None


def decimal_odds(text):
    text = str(text or "").strip()
    try:
        if "/" in text:
            a, b = text.split("/", 1)
            numerator, denominator = float(a), float(b)
            if numerator <= 0 or denominator <= 0:
                return None
            value = 1 + numerator / denominator
        else:
            value = float(text)
        return value if math.isfinite(value) and value > 1 else None
    except (ValueError, ZeroDivisionError):
        return None


def classify(points_offered, seconds, margin):
    if margin > 8:
        return "SKIP", "Margin exceeds 8 points"
    if points_offered <= 17:
        return "SKIP", "Points offered are 17 or fewer"
    if 19 <= points_offered <= 22:
        # At the 3:20 trigger the original <=60-second OT condition cannot apply.
        return "PLAY_UNDER", "19–22 points offered; margin at most 8"
    if abs(points_offered - 18) < 0.6:
        return "BORDERLINE", "About 18 points offered"
    return "BORDERLINE", "Outside the play range"


@contextmanager
def connect():
    """Commit or roll back each operation and always close the connection."""
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, endpoint TEXT NOT NULL,
            http_status INTEGER, ok INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, period INTEGER, seconds_left INTEGER,
            home_score INTEGER, away_score INTEGER, final_home INTEGER,
            final_away INTEGER, final_total INTEGER, result_status TEXT DEFAULT 'pending',
            last_result_check REAL
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, at REAL NOT NULL,
            period INTEGER, seconds_left INTEGER, home_score INTEGER, away_score INTEGER,
            total_line REAL, under_odds TEXT, points_offered REAL, margin INTEGER,
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );
        CREATE INDEX IF NOT EXISTS observations_event_clock ON observations(event_id, period, seconds_left);
        CREATE TABLE IF NOT EXISTS decisions (
            event_id TEXT PRIMARY KEY, at REAL NOT NULL, decision TEXT NOT NULL,
            reason TEXT NOT NULL, seconds_left INTEGER, home_score INTEGER,
            away_score INTEGER, total_line REAL, under_odds TEXT,
            points_offered REAL, margin INTEGER, outcome TEXT DEFAULT 'pending',
            pnl_units REAL, FOREIGN KEY(event_id) REFERENCES events(event_id)
        );
        """)
        db.execute(
            """UPDATE decisions SET reason = CASE WHEN EXISTS (
                SELECT 1 FROM observations o WHERE o.event_id = decisions.event_id
                AND o.period = 4 AND o.seconds_left BETWEEN ? AND ? AND o.at <= decisions.at
            ) THEN ? ELSE ? END
            WHERE decision = 'MISSED' AND reason = '3:20 window passed without a usable line'""",
            (WINDOW_CLOSE, WINDOW_OPEN, MISSED_MARKET, MISSED_CLOCK),
        )


def meta_get(key):
    with connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def meta_set(key, value):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


def record_event(event_id, name, p, seconds, home, away):
    stamp = now()
    with connect() as db:
        db.execute(
            """INSERT INTO events(event_id,name,first_seen,last_seen,period,seconds_left,home_score,away_score)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
            name=excluded.name,last_seen=excluded.last_seen,period=excluded.period,
            seconds_left=excluded.seconds_left,home_score=excluded.home_score,away_score=excluded.away_score""",
            (event_id, name, stamp, stamp, p, seconds, home, away),
        )


def record_observation(event_id, p, seconds, home, away, line, odds):
    points = line - home - away if None not in (line, home, away) else None
    margin = abs(home - away) if None not in (home, away) else None
    with connect() as db:
        db.execute(
            """INSERT INTO observations
            (event_id,at,period,seconds_left,home_score,away_score,total_line,under_odds,points_offered,margin)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_id, now(), p, seconds, home, away, line, odds, points, margin),
        )
    return points, margin


def decision_exists(event_id):
    with connect() as db:
        return db.execute("SELECT 1 FROM decisions WHERE event_id=?", (event_id,)).fetchone() is not None


def record_decision(event_id, label, reason, seconds, home, away, line, odds, points, margin):
    with connect() as db:
        result = db.execute(
            """INSERT OR IGNORE INTO decisions
            (event_id,at,decision,reason,seconds_left,home_score,away_score,total_line,under_odds,points_offered,margin)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, now(), label, reason, seconds, home, away, line, odds, points, margin),
        )
    if result.rowcount:
        clock = f"{seconds // 60}:{seconds % 60:02d}" if seconds is not None else "—"
        logger.info("Decision FI=%s | %s | Q4 %s | %s", event_id, label, clock, reason)


def record_missed_window(event_id, seconds=None, home=None, away=None):
    with connect() as db:
        in_window = db.execute(
            """SELECT 1 FROM observations WHERE event_id = ? AND period = 4
            AND seconds_left BETWEEN ? AND ? LIMIT 1""",
            (event_id, WINDOW_CLOSE, WINDOW_OPEN),
        ).fetchone()
    reason = MISSED_MARKET if in_window else MISSED_CLOCK
    margin = abs(home - away) if home is not None and away is not None else None
    record_decision(event_id, "MISSED", reason, seconds, home, away, None, None, None, margin)


def settle(event_id, final_home, final_away):
    total = final_home + final_away
    with connect() as db:
        db.execute(
            """UPDATE events SET final_home=?,final_away=?,final_total=?,result_status='final'
            WHERE event_id=?""",
            (final_home, final_away, total, event_id),
        )
        row = db.execute(
            "SELECT decision,total_line,under_odds FROM decisions WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            return
        if row["decision"] != "PLAY_UNDER" or row["total_line"] is None:
            outcome, pnl = "not_picked", None
        elif total < row["total_line"]:
            odds = decimal_odds(row["under_odds"])
            outcome, pnl = "win", odds - 1 if odds else None
        elif total == row["total_line"]:
            outcome, pnl = "push", 0.0
        else:
            outcome, pnl = "loss", -1.0
        db.execute("UPDATE decisions SET outcome=?,pnl_units=? WHERE event_id=?", (outcome, pnl, event_id))


class ApiClient:
    def __init__(self, token):
        self.token = token
        self.thread_local = threading.local()
        self.lock = threading.Lock()
        self.blocked_until = 0

    def get(self, endpoint, params=None):
        with self.lock:
            stamp = now()
            if stamp < self.blocked_until:
                return None
            with connect() as db:
                used = db.execute("SELECT count(*) FROM calls WHERE at>=?", (stamp - 3600,)).fetchone()[0]
                if used < HOURLY_BUDGET:
                    call_id = db.execute(
                        "INSERT INTO calls(at,endpoint,ok) VALUES(?,?,0)", (stamp, endpoint)
                    ).lastrowid
            if used >= HOURLY_BUDGET:
                meta_set(
                    "last_error", "Local hourly request budget reached; waiting for older calls to expire"
                )
                return None
        if not hasattr(self.thread_local, "session"):
            self.thread_local.session = requests.Session()
        status, payload, ok = None, None, False
        request_name = endpoint
        if endpoint == "event" and params:
            request_name += f" FI={params.get('FI')}"
        logger.info("Request %s", request_name)
        started = time.monotonic()
        try:
            response = self.thread_local.session.get(
                BASE_URL + "/" + endpoint,
                params={**(params or {}), "token": self.token},
                timeout=2 if endpoint == "event" else 10,
            )
            status = response.status_code
            remaining = integer(response.headers.get("X-RateLimit-Remaining"))
            provider_limit = integer(response.headers.get("X-RateLimit-Limit"))
            reset = integer(response.headers.get("X-RateLimit-Reset"))
            if reset is not None:
                meta_set("provider_reset", reset)
            if remaining is not None:
                meta_set("provider_remaining", remaining)
            if provider_limit is not None:
                meta_set("provider_limit", provider_limit)
            with self.lock:
                if remaining is not None and remaining < 20 and reset:
                    self.blocked_until = max(self.blocked_until, reset + 2)
                if status == 429:
                    self.blocked_until = max(self.blocked_until, now() + 60)
            response.raise_for_status()
            payload = response.json()
            ok = isinstance(payload, dict) and payload.get("success") in (1, True, "1")
            if not ok:
                meta_set("last_error", "API returned an unsuccessful response for " + endpoint)
        except (requests.RequestException, ValueError):
            # Never log the exception: requests can include the token-bearing URL.
            meta_set("last_error", "Request failed for " + endpoint + (f" (HTTP {status})" if status else ""))
        http = f"HTTP {status}" if status is not None else "no HTTP response"
        logger.info(
            "Response %s | %s | %s | %.3fs",
            request_name,
            http,
            "OK" if ok else "failed",
            time.monotonic() - started,
        )
        with connect() as db:
            db.execute("UPDATE calls SET http_status=?,ok=? WHERE id=?", (status, int(ok), call_id))
        if ok:
            meta_set("last_success", stamp)
            meta_set("last_error", "")
        return payload if ok else None


def event_snapshot(rows, fallback=None):
    ev = next((row for row in rows if row.get("type") == "EV"), None)
    if not ev:
        ev = fallback or {}
    p = period(ev.get("CP") or ev.get("period") or ev.get("ED"))
    seconds = remaining_seconds(ev)
    home, away = score(ev.get("SS") or ev.get("ss"))
    return p, seconds, home, away


def poll_interval(seconds):
    if seconds is None:
        return 5
    if seconds <= WINDOW_POLL_START:
        return WINDOW_POLL_SECONDS
    # Check slowly early in Q4, then switch to fast polling ahead of 3:27.
    return min(20, max(FAST_POLL_SECONDS, seconds - FAST_POLL_START))


@dataclass
class QuarterWatch:
    name: str
    seconds: int | None
    next_poll: float
    last_poll: float = 0


def discover_games(api):
    filtered = api.get("inplay_filter", {"sport_id": SPORT_ID})
    listing = api.get("inplay") if filtered is not None else None
    if listing is None:
        return None
    return filter_events(filtered), inplay_events(listing)


def refresh_watches(discovery, watches, closed_windows):
    if discovery is None:
        return
    league_games, visible = discovery
    for event_id, basic in league_games.items():
        ev = visible.get(event_id)
        if ev is None:
            continue
        p, seconds, home, away = event_snapshot([ev])
        home_name = (basic.get("home") or {}).get("name", "")
        away_name = (basic.get("away") or {}).get("name", "")
        name = str(ev.get("NA") or f"{home_name} vs {away_name}")
        record_event(event_id, name, p, seconds, home, away)
        if p != 4:
            watches.pop(event_id, None)
            continue
        if event_id in closed_windows:
            continue
        if seconds is not None and seconds < WINDOW_CLOSE:
            if not decision_exists(event_id):
                record_missed_window(event_id, seconds, home, away)
            closed_windows.add(event_id)
            watches.pop(event_id, None)
        elif event_id not in watches:
            watches[event_id] = QuarterWatch(name, seconds, now())
        elif seconds is not None and seconds <= FAST_POLL_START:
            watch = watches[event_id]
            watch.seconds = min(watch.seconds, seconds) if watch.seconds is not None else seconds
            watch.next_poll = min(watch.next_poll, max(now(), watch.last_poll + poll_interval(watch.seconds)))
    for event_id in watches.keys() - league_games.keys() - visible.keys():
        del watches[event_id]
    meta_set("games_seen", len(league_games))


def apply_detail(event_id, watch, payload):
    """Record a Q4 snapshot and return whether its decision window has closed."""
    rows = records(payload)
    p, seconds, home, away = event_snapshot(rows)
    if p is not None and p > 4:
        record_event(event_id, watch.name, p, seconds, home, away)
        if not decision_exists(event_id):
            record_missed_window(event_id)
        return True
    if p != 4 or seconds is None:
        return False
    watch.seconds = min(watch.seconds, seconds) if watch.seconds is not None else seconds
    record_event(event_id, watch.name, p, seconds, home, away)
    line, odds, _ = full_game_under(rows)
    points, margin = record_observation(event_id, p, seconds, home, away, line, odds)
    price = decimal_odds(odds)
    logger.info(
        "Q4 snapshot FI=%s | %s | %d:%02d | score %s–%s | line %s | Under %s",
        event_id,
        watch.name,
        seconds // 60,
        seconds % 60,
        home,
        away,
        line if line is not None else "—",
        f"{price:.2f}" if price is not None else "—",
    )
    if seconds < WINDOW_CLOSE:
        if not decision_exists(event_id):
            record_missed_window(event_id, seconds, home, away)
        return True
    if seconds <= WINDOW_OPEN and points is not None and not decision_exists(event_id):
        label, reason = classify(points, seconds, margin)
        record_decision(event_id, label, reason, seconds, home, away, line, odds, points, margin)
    return False


def pending_result_ids():
    stamp = now()
    with connect() as db:
        pending = db.execute(
            """SELECT event_id FROM events
            WHERE result_status!='final' AND (last_result_check IS NULL OR last_result_check<?)
            AND (last_seen<? OR (period=4 AND seconds_left=0)) LIMIT 10""",
            (stamp - 120, stamp - 90),
        ).fetchall()
        ids = [row["event_id"] for row in pending]
        db.executemany(
            "UPDATE events SET last_result_check=? WHERE event_id=?", [(stamp, event_id) for event_id in ids]
        )
    return ids


def engine(token):
    api = ApiClient(token)
    start_text = meta_get("trial_start")
    if start_text is None:
        meta_set("trial_start", now())
        start_text = meta_get("trial_start")
    deadline = float(start_text) + TRIAL_SECONDS
    meta_set("status", "running")
    next_discovery = next_result_scan = 0
    discovery_job = result_job = None
    result_ids = []
    detail_jobs = {}
    watches = {}
    closed_windows = set()
    # Discovery and settlement have their own slots so slow calls cannot stall Q4 polls.
    with ThreadPoolExecutor(max_workers=MAX_DETAIL_REQUESTS + 2) as requests_pool:
        while now() < deadline:
            for event_id, job in list(detail_jobs.items()):
                if not job.done():
                    continue
                del detail_jobs[event_id]
                watch = watches.get(event_id)
                if watch is None:
                    continue
                if apply_detail(event_id, watch, job.result()):
                    closed_windows.add(event_id)
                    del watches[event_id]
                else:
                    watch.next_poll = watch.last_poll + poll_interval(watch.seconds)

            if discovery_job is not None and discovery_job.done():
                refresh_watches(discovery_job.result(), watches, closed_windows)
                discovery_job = None
            if result_job is not None and result_job.done():
                for item in records(result_job.result()):
                    event_id = result_event_id(item)
                    if event_id in result_ids and str(item.get("time_status")) == "3":
                        home, away = score(item.get("ss"))
                        if home is not None:
                            if not decision_exists(event_id):
                                record_missed_window(event_id)
                            settle(event_id, home, away)
                            closed_windows.add(event_id)
                            watches.pop(event_id, None)
                result_job = None

            stamp = now()
            due = [
                (event_id, watch)
                for event_id, watch in watches.items()
                if event_id not in detail_jobs and stamp >= watch.next_poll
            ]
            due.sort(key=lambda entry: (poll_interval(entry[1].seconds), entry[1].next_poll))
            for event_id, watch in due[: MAX_DETAIL_REQUESTS - len(detail_jobs)]:
                watch.last_poll = now()
                detail_jobs[event_id] = requests_pool.submit(api.get, "event", {"FI": event_id})

            if discovery_job is None and stamp >= next_discovery:
                discovery_job = requests_pool.submit(discover_games, api)
                next_discovery = stamp + DISCOVERY_SECONDS
            if result_job is None and stamp >= next_result_scan:
                result_ids = pending_result_ids()
                if result_ids:
                    result_job = requests_pool.submit(api.get, "result", {"event_id": ",".join(result_ids)})
                next_result_scan = stamp + 5
            time.sleep(0.1 if watches else 0.5)
    meta_set("status", "complete")


def resilient_engine(token):
    """Keep the trial alive if an unexpected payload or database error occurs."""
    while True:
        try:
            engine(token)
            return
        except Exception as exc:
            # Deliberately omit exception text; HTTP libraries may include the token URL.
            logger.error("Trial worker stopped on %s. Retrying in 15 seconds.", type(exc).__name__)
            try:
                meta_set("last_error", "Worker retrying after " + type(exc).__name__)
                meta_set("status", "recovering")
                start = meta_get("trial_start")
                if start and now() >= float(start) + TRIAL_SECONDS:
                    meta_set("status", "complete")
                    return
            except sqlite3.Error as db_error:
                logger.error("Trial database unavailable during recovery (%s).", type(db_error).__name__)
            time.sleep(15)


def outcome_fields(row):
    picked = row["decision"] == "PLAY_UNDER"
    if row["decision"] == "MISSED" or row["total_line"] is None:
        result = "unavailable"
    elif row["final_total"] is None:
        result = "pending"
    elif row["final_total"] < row["total_line"]:
        result = "hit"
    elif row["final_total"] > row["total_line"]:
        result = "miss"
    else:
        result = "push"
    return {"picked": picked, "line_result": result}


def state():
    stamp = now()
    with connect() as db:
        calls_hour = db.execute("SELECT count(*) FROM calls WHERE at>=?", (stamp - 3600,)).fetchone()[0]
        calls_total = db.execute("SELECT count(*) FROM calls").fetchone()[0]
        tracked_games = db.execute("SELECT count(*) FROM events").fetchone()[0]
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY last_seen DESC LIMIT 500")]
        decisions = [
            dict(row)
            for row in db.execute("""SELECT d.*,e.name,e.final_total,e.final_home,e.final_away
            FROM decisions d JOIN events e USING(event_id) ORDER BY d.at DESC LIMIT 200""")
        ]
        observations = [
            dict(row)
            for row in db.execute("""SELECT o.*,e.name FROM observations o
            JOIN events e USING(event_id) ORDER BY o.at DESC LIMIT 60""")
        ]
        totals = dict(
            db.execute("""SELECT
            count(*) AS decisions,
            sum(decision='PLAY_UNDER') AS picks,
            sum(decision='SKIP') AS skips,
            sum(decision='BORDERLINE') AS borderline,
            sum(decision='MISSED') AS missed,
            sum(outcome='win') AS wins,
            sum(outcome='loss') AS losses,
            sum(outcome='push') AS pushes,
            sum(decision='PLAY_UNDER' AND outcome='pending') AS pending,
            sum(pnl_units) AS pnl_units FROM decisions""").fetchone()
        )
    for row in decisions + observations:
        row["under_odds_decimal"] = decimal_odds(row["under_odds"])
    for row in decisions:
        row.update(outcome_fields(row))
    start = meta_get("trial_start")
    return {
        "mode": meta_get("mode") or "live",
        "status": meta_get("status") or "awaiting_key",
        "trial_start": float(start) if start else None,
        "trial_end": float(start) + TRIAL_SECONDS if start else None,
        "now": stamp,
        "last_success": float(meta_get("last_success")) if meta_get("last_success") else None,
        "last_error": meta_get("last_error") or "",
        "games_seen": int(meta_get("games_seen") or 0),
        "tracked_games": tracked_games,
        "calls_hour": calls_hour,
        "calls_total": calls_total,
        "hourly_budget": HOURLY_BUDGET,
        "provider_remaining": integer(meta_get("provider_remaining")),
        "provider_limit": integer(meta_get("provider_limit")),
        "provider_reset": integer(meta_get("provider_reset")),
        "events": events,
        "decisions": decisions,
        "observations": observations,
        "totals": totals,
    }


EXPORTS = {
    "events": "SELECT * FROM events ORDER BY first_seen",
    "decisions": "SELECT d.*,e.name,e.final_total FROM decisions d JOIN events e USING(event_id) ORDER BY d.at",
    "observations": "SELECT * FROM observations ORDER BY at",
    "calls": "SELECT * FROM calls ORDER BY at",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body, content_type = HTML_PATH.read_bytes(), "text/html; charset=utf-8"
        elif path in ("/styles.css", "/app.js"):
            body = (ROOT / "web" / path.lstrip("/")).read_bytes()
            content_type = "text/css" if path.endswith(".css") else "text/javascript"
        elif path == "/api/state":
            try:
                body = json.dumps(state()).encode()
            except sqlite3.Error:
                self.send_error(503, "Trial data temporarily unavailable")
                return
            content_type = "application/json"
        elif path.startswith("/export/") and path.removeprefix("/export/").removesuffix(".csv") in EXPORTS:
            name = path.removeprefix("/export/").removesuffix(".csv")
            with connect() as db:
                cursor = db.execute(EXPORTS[name])
                output = io.StringIO()
                fields = [col[0] for col in cursor.description]
                if name == "decisions":
                    fields.extend(("picked", "line_result"))
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for result in cursor:
                    row = dict(result)
                    if name == "decisions":
                        row.update(outcome_fields(row))
                    if "under_odds" in row:
                        odds = decimal_odds(row["under_odds"])
                        row["under_odds"] = f"{odds:.2f}" if odds is not None else None
                    writer.writerow(row)
            body, content_type = output.getvalue().encode(), "text/csv; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def watch_terminal():
    """Follow the active trial's saved snapshots and decisions without starting a worker."""
    queries = {
        "observations": """SELECT o.id AS row_number, o.at, e.name, 'Q4 snapshot' AS label,
            o.seconds_left, o.home_score, o.away_score, o.total_line, o.under_odds, NULL AS reason
            FROM observations o JOIN events e USING(event_id) WHERE o.id > ? ORDER BY o.id""",
        "decisions": """SELECT d.rowid AS row_number, d.at, e.name, d.decision AS label,
            d.seconds_left, d.home_score, d.away_score,
            CASE WHEN d.decision != 'MISSED' THEN d.total_line END AS total_line,
            CASE WHEN d.decision != 'MISSED' THEN d.under_odds END AS under_odds, d.reason
            FROM decisions d JOIN events e USING(event_id) WHERE d.rowid > ? ORDER BY d.rowid""",
    }
    if not DB_PATH.is_file():
        raise SystemExit(f"Trial database not found at {DB_PATH}")

    db = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        cursor = {
            "observations": max(
                0, db.execute("SELECT coalesce(max(id),0) FROM observations").fetchone()[0] - 5
            ),
            "decisions": max(0, db.execute("SELECT coalesce(max(rowid),0) FROM decisions").fetchone()[0] - 3),
        }
        print(
            "Watching saved Q4 snapshots and decisions. Timestamps are local. Press Ctrl+C to exit.",
            flush=True,
        )
        while True:
            new_rows = []
            for table, query in queries.items():
                rows = db.execute(query, (cursor[table],)).fetchall()
                if rows:
                    cursor[table] = rows[-1]["row_number"]
                    new_rows.extend(rows)
            for row in sorted(new_rows, key=lambda item: item["at"]):
                stamp = datetime.fromtimestamp(row["at"]).isoformat(sep=" ", timespec="milliseconds")
                seconds = row["seconds_left"]
                game_clock = f"{seconds // 60}:{seconds % 60:02d}" if seconds is not None else "—"
                details = [row["name"], f"Q4 {game_clock}"]
                if row["home_score"] is not None and row["away_score"] is not None:
                    details.append(f"score {row['home_score']}–{row['away_score']}")
                if row["total_line"] is not None:
                    details.append(f"line {row['total_line']:g}")
                odds = decimal_odds(row["under_odds"])
                if odds is not None:
                    details.append(f"Under {odds:.2f}")
                if row["reason"]:
                    details.append(row["reason"])
                print(f"{stamp}  [{row['label']}]  " + "  |  ".join(details), flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopped watching.")
    finally:
        db.close()


def default_data_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QuarterWatch"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "QuarterWatch"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "quarterwatch"


def main():
    global DB_PATH, TRIAL_SECONDS
    parser = argparse.ArgumentParser(description="QuarterWatch live monitor and paper-trial dashboard")
    parser.add_argument("--demo", action="store_true", help="Run a synthetic replay without network calls")
    parser.add_argument("--key-file", type=Path, help="Private file containing a BetsAPI token")
    parser.add_argument("--data-dir", type=Path, help="Directory for the SQLite database")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--hours", type=float, default=24, help="Live trial duration in hours")
    parser.add_argument("--watch", action="store_true", help="Follow the saved live trial in this terminal")
    args = parser.parse_args()
    if args.hours <= 0 or not math.isfinite(args.hours):
        parser.error("--hours must be a positive finite number")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    data_dir = args.data_dir or default_data_dir() / ("demo" if args.demo else "live")
    DB_PATH = data_dir.expanduser().resolve() / "trial.sqlite3"
    TRIAL_SECONDS = args.hours * 3600
    if args.watch:
        watch_terminal()
        return
    token = os.environ.get("BETSAPI_TOKEN", "").strip()
    if args.key_file and not args.demo:
        try:
            token = args.key_file.expanduser().read_text().strip()
        except OSError:
            parser.error("Could not read --key-file")
    if not args.demo and not token:
        parser.error("Set BETSAPI_TOKEN or pass --key-file. For a key-free preview, use --demo")
    initialize()
    stored_duration = meta_get("trial_duration")
    if stored_duration:
        TRIAL_SECONDS = float(stored_duration)
    else:
        meta_set("trial_duration", TRIAL_SECONDS)
    meta_set("mode", "demo" if args.demo else "live")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    if args.demo:
        from demo import run_demo

        target, arguments = run_demo, (sys.modules[__name__],)
    else:
        target, arguments = resilient_engine, (token,)
    threading.Thread(target=target, args=arguments, daemon=True).start()
    print(f"QuarterWatch • {'synthetic demo' if args.demo else 'live trial'}", flush=True)
    print(f"Dashboard → http://127.0.0.1:{args.port}", flush=True)
    print(f"Data → {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nQuarterWatch stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
