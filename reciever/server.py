"""HVAC receiver with local SQLite storage (Python standard library only).

Run: python server.py
Inspect saved rows: python server.py --recent 10
The database is data/hvac.db beside this script, regardless of working directory.
Keep it on the local PC. Incoming battery_v is stored as reported, including
readings taken without a battery; it is not an estimate of charge percentage.
"""

import argparse
import json
import math
import os
import threading
import urllib.request
import urllib.error
from urllib.parse import urlsplit
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# Initial defaults only. After first startup, alert_settings in SQLite is authoritative.
TEMP_MIN, TEMP_MAX = 20.0, 26.0
RH_MIN, RH_MAX = 40.0, 60.0

# Credentials stay in the process environment, never in the database or source.
SLACK_ENABLED = False
SLACK_TIMEOUT = 8

# Only these Slack users may submit threshold changes. Checked again on save.
SETTINGS_EDITORS = frozenset({"U09989EJ0D8", "U089R611C9X"})
SETTING_FIELDS = ("temp_min", "temp_max", "rh_min", "rh_max")

DB_PATH = Path(__file__).resolve().parent / "data" / "hvac.db"


def init_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY,
                    received_at_utc TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    humidity_rh REAL NOT NULL,
                    battery_v REAL,
                    source_ip TEXT NOT NULL
                )
            """)
            conn.execute("""CREATE TABLE IF NOT EXISTS alert_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), temp_min REAL NOT NULL,
                temp_max REAL NOT NULL, rh_min REAL NOT NULL, rh_max REAL NOT NULL,
                version INTEGER NOT NULL, updated_at_utc TEXT NOT NULL,
                updated_by TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO alert_settings VALUES (1, ?, ?, ?, ?, 1, ?, ?)",
                         (TEMP_MIN, TEMP_MAX, RH_MIN, RH_MAX,
                          datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "initial defaults"))
            conn.execute("""CREATE TABLE IF NOT EXISTS settings_audit (
                id INTEGER PRIMARY KEY, changed_at_utc TEXT NOT NULL,
                changed_by TEXT NOT NULL, old_settings TEXT NOT NULL,
                new_settings TEXT NOT NULL, version INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE)""")
            # Separate identity table: changing a name never rewrites historical readings.
            # IF NOT EXISTS allows safe upgrades of an existing database.
            conn.execute("""CREATE TABLE IF NOT EXISTS sensors (
                device_id TEXT PRIMARY KEY, name TEXT NOT NULL)""")
            conn.execute("""INSERT OR IGNORE INTO sensors SELECT DISTINCT
                device_id, device_id FROM readings""")
            conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY, reading_id INTEGER NOT NULL,
                received_at_utc TEXT NOT NULL, device_id TEXT NOT NULL,
                metric TEXT NOT NULL, status TEXT NOT NULL, value REAL NOT NULL,
                FOREIGN KEY(reading_id) REFERENCES readings(id))""")
            # Add a reason without dropping existing alerts or readings.
            if "reason" not in {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}:
                conn.execute("ALTER TABLE alerts ADD COLUMN reason TEXT NOT NULL DEFAULT 'reading'")
            # Durable delivery queue. A crash/restart keeps unsent messages.
            # Existing alerts are deliberately not backfilled into this queue.
            conn.execute("""CREATE TABLE IF NOT EXISTS slack_outbox (
                id INTEGER PRIMARY KEY, message TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                sent_at_utc TEXT, last_error TEXT)""")
            # Last condition is durable, so restarting the server does not spam alerts.
            conn.execute("""CREATE TABLE IF NOT EXISTS alert_state (
                device_id TEXT NOT NULL, metric TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(device_id, metric))""")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS readings_device_time
                ON readings(device_id, received_at_utc)
            """)


def validate_reading(data):
    """Reject malformed data; valid readings outside comfort bounds are still saved."""
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    device = data.get("device_id")
    if not isinstance(device, str) or not device.strip() or len(device) > 128:
        raise ValueError("device_id must be a nonempty string (max 128 characters)")
    for field in ("temperature_c", "humidity_rh", "battery_v"):
        value = data.get(field)
        if field == "battery_v" and value is None:
            continue  # Older firmware may omit battery readings.
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError(f"{field} must be a finite number")
    if not 0 <= data["humidity_rh"] <= 100:
        raise ValueError("humidity_rh must be between 0 and 100")
    if data.get("battery_v") is not None and data["battery_v"] < 0:
        raise ValueError("battery_v must not be negative")


def save_reading(data, source_ip):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    # Each request owns its connection: safe for ThreadingHTTPServer.
    # The context commits before HTTP 200 is returned, then closing releases it.
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            # Acquire the writer lock before reading/updating alert state. Concurrent
            # HTTP requests cannot both announce the same condition transition.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO sensors VALUES (?, ?)",
                         (data["device_id"], data["device_id"]))
            cursor = conn.execute("""
                INSERT INTO readings
                    (received_at_utc, device_id, temperature_c, humidity_rh,
                     battery_v, source_ip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (timestamp, data["device_id"], data["temperature_c"],
                  data["humidity_rh"], data.get("battery_v"), source_ip))
            row_id = cursor.lastrowid
            settings = read_settings(conn)
            events, name = evaluate_reading(conn, row_id, data, timestamp, timestamp,
                                            settings, "reading", SLACK_ENABLED)
    # Log only after the entire transaction committed successfully.
    for event in events:
        print(f"[ALERT/RECOVERY] {timestamp} {name} ({data['device_id']}): {event}", flush=True)
    return row_id, timestamp


def read_settings(conn=None):
    """Read fresh on every use; shared SQLite makes changes visible across processes."""
    if conn is None:
        with closing(sqlite3.connect(DB_PATH, timeout=0.5)) as connection:
            return read_settings(connection)
    row = conn.execute("SELECT temp_min, temp_max, rh_min, rh_max, version, "
                       "updated_at_utc, updated_by FROM alert_settings WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Settings not initialized. Start the updated server first.")
    return dict(zip((*SETTING_FIELDS, "version", "updated_at_utc", "updated_by"), row))


def validate_settings(values):
    """Return field-specific errors for Slack's modal; reject NaN and infinity."""
    errors = {}
    for field in SETTING_FIELDS:
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors[field] = "Enter a finite number."
    if errors:
        return errors
    if values["temp_min"] < -273.15:
        errors["temp_min"] = "Temperature cannot be below -273.15 C."
    if values["temp_min"] >= values["temp_max"]:
        errors["temp_max"] = "Maximum must be greater than minimum."
    for field in ("rh_min", "rh_max"):
        if not 0 <= values[field] <= 100:
            errors[field] = "Relative humidity must be between 0 and 100%."
    if values["rh_min"] >= values["rh_max"]:
        errors["rh_max"] = "Maximum must be greater than minimum."
    return errors


def evaluate_reading(conn, row_id, data, observed_at, evaluated_at, settings, reason, notify):
    """Update state and queue transitions inside the caller's transaction.

    Re-evaluation references an existing reading; it never fabricates a new sample.
    A recovery for one metric does not imply both metrics are normal.
    """
    events = []
    for metric, low, high in (("temperature_c", settings["temp_min"], settings["temp_max"]),
                              ("humidity_rh", settings["rh_min"], settings["rh_max"])):
        value = data[metric]
        status = "low" if value < low else "high" if value > high else "normal"
        previous = conn.execute("SELECT status FROM alert_state WHERE device_id=? AND metric=?",
                                (data["device_id"], metric)).fetchone()
        if status != (previous[0] if previous else "normal"):
            conn.execute("INSERT INTO alerts (reading_id, received_at_utc, device_id, metric, "
                         "status, value, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (row_id, evaluated_at, data["device_id"], metric, status, value, reason))
            events.append(f"{metric}: {status} ({value}; bounds {low} to {high})")
        conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?, ?, ?)",
                     (data["device_id"], metric, status))
    row = conn.execute("SELECT name FROM sensors WHERE device_id=?", (data["device_id"],)).fetchone()
    name = row[0] if row else data["device_id"]
    if events and notify:
        message = (f"HVAC alert / recovery — {name}\n"
                   f"Device: {data['device_id'].replace(':', '-')}\n"
                   f"Last reading (UTC): {observed_at}\n"
                   f"Evaluated (UTC): {evaluated_at}\n"
                   f"Reason: {reason}\n"
                   f"Temperature: {data['temperature_c']:.2f} C "
                   f"(allowed {settings['temp_min']}-{settings['temp_max']})\n"
                   f"RH: {data['humidity_rh']:.2f}% "
                   f"(allowed {settings['rh_min']}-{settings['rh_max']})\n" + "\n".join(events))
        conn.execute("INSERT INTO slack_outbox (message) VALUES (?)", (message,))
    return events, name


class SettingsConflict(ValueError):
    pass


def update_settings(values, user_id, expected_version, request_id, notify=False):
    """Atomic settings/audit/re-evaluation, with authorization and stale-form guard.

    Short lock timeout and SQL deadline allow Slack to receive ack within 3 sec.
    A duplicate view submission cannot produce a duplicate audit or notification.
    """
    if user_id not in SETTINGS_EDITORS:
        raise PermissionError("Only the two configured project editors may change settings.")
    errors = validate_settings(values)
    if errors:
        raise ValueError(" ".join(errors.values()))
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Missing settings request ID.")
    import time
    deadline = time.monotonic() + 1.5
    with closing(sqlite3.connect(DB_PATH, timeout=0.5)) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute("SELECT version FROM settings_audit WHERE request_id=?",
                                     (request_id,)).fetchone()
            if duplicate:
                return {"version": duplicate[0], "changed": False, "duplicate": True,
                        "transitions": 0}
            old = read_settings(conn)
            if old["version"] != expected_version:
                raise SettingsConflict("Settings changed since this form opened. Close it and reopen Settings.")
            if all(old[key] == values[key] for key in SETTING_FIELDS):
                return {"version": old["version"], "changed": False, "transitions": 0}
            version = old["version"] + 1
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            conn.execute("UPDATE alert_settings SET temp_min=?, temp_max=?, rh_min=?, rh_max=?, "
                         "version=?, updated_at_utc=?, updated_by=? WHERE id=1",
                         (*[values[key] for key in SETTING_FIELDS], version, now, user_id))
            conn.execute("INSERT INTO settings_audit (changed_at_utc, changed_by, old_settings, "
                         "new_settings, version, request_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (now, user_id, json.dumps({k: old[k] for k in SETTING_FIELDS}),
                          json.dumps({k: values[k] for k in SETTING_FIELDS}), version, request_id))
            rows = conn.execute("SELECT r.id, r.received_at_utc, r.device_id, r.temperature_c, "
                                "r.humidity_rh FROM readings r JOIN "
                                "(SELECT MAX(id) AS id FROM readings GROUP BY device_id) latest "
                                "ON r.id=latest.id").fetchall()
            transitions = 0
            for row in rows:
                data = dict(zip(("device_id", "temperature_c", "humidity_rh"), row[2:]))
                events, _ = evaluate_reading(conn, row[0], data, row[1], now, values,
                                             "threshold change: re-evaluated last stored reading", notify)
                transitions += len(events)
    return {"version": version, "changed": True, "transitions": transitions}


def slack_url_from_environment():
    """Accept only Slack HTTPS webhook endpoints; never print this secret URL."""
    url = os.environ.get("HVAC_SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in
            ("hooks.slack.com", "hooks.slack-gov.com") or
            parts.netloc != parts.hostname or not parts.path.startswith("/services/")
            or len(parts.path.split("/")) != 5 or
            not all(parts.path.split("/")[2:]) or parts.query or parts.fragment):
        raise ValueError("HVAC_SLACK_WEBHOOK_URL must be a Slack HTTPS incoming webhook URL")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a webhook payload to an unexpected destination.
        return None


def send_slack(url, message):
    """Return a sanitized error or None. Do not log exceptions containing the URL.

    Explicit plain_text prevents sensor names from creating Slack mentions.
    A timeout can happen after Slack accepted the message: retries may duplicate it.
    """
    body = json.dumps({"text": "HVAC alert / recovery", "blocks": [
        {"type": "section", "text": {"type": "plain_text", "text": message}}
    ]}).encode("utf-8")
    request = urllib.request.Request(url, data=body,
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=SLACK_TIMEOUT) as response:
            if response.status == 200 and response.read(32).strip() == b"ok":
                return None
            return "Unexpected Slack response"
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return f"Slack HTTP {status}"
    except (OSError, urllib.error.URLError, ValueError):
        return "Slack connection failed or timed out"


def deliver_one_slack(url):
    """One worker owns delivery. Network I/O never holds a SQLite writer lock."""
    import time
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        row = conn.execute("SELECT id, message FROM slack_outbox WHERE sent_at_utc IS NULL "
                           "AND next_attempt <= ? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
    if row is None:
        return False
    error = send_slack(url, row[1])
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("UPDATE slack_outbox SET attempts=attempts+1, next_attempt=?, "
                         "sent_at_utc=?, last_error=? WHERE id=?",
                         (time.time() + 60,
                          None if error else datetime.now(timezone.utc).isoformat(), error, row[0]))
    print(f"[SLACK] Message #{row[0]}: " +
          (f"{error}; retry in 60 seconds" if error else "sent"), flush=True)
    return True


def slack_worker(url, stop):
    while not stop.is_set():
        try:
            busy = deliver_one_slack(url)
        except sqlite3.Error:
            print("[SLACK] Queue database unavailable; will retry", flush=True)
            busy = False
        stop.wait(1 if busy else 2)


def normalized_time(text):
    """Require an explicit UTC offset; convert to the same format used for storage."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Time must include offset, e.g. 2026-10-14T12:00:00-04:00")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def show_recent(limit, device=None, name=None, start=None, end=None):
    """Parameterized SQL keeps user values separate from SQL instructions.

    Time range is [start, end): include the start, exclude the end.
    Names may be duplicated, so use device_id when selecting one exact board.
    """
    clauses, values = [], []
    for column, value, op in (("r.device_id", device, "="), ("s.name", name, "="),
                              ("r.received_at_utc", start, ">="),
                              ("r.received_at_utc", end, "<")):
        if value is not None:
            clauses.append(f"{column} {op} ?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute("SELECT r.id, r.received_at_utc, r.device_id, s.name, "
                            "r.temperature_c, r.humidity_rh, r.battery_v "
                            "FROM readings r JOIN sensors s ON r.device_id=s.device_id" +
                            where + " ORDER BY r.received_at_utc DESC, r.id DESC LIMIT ?",
                            [*values, limit]).fetchall()
    print(f"Database: {DB_PATH}")
    print("ID | Time (UTC) | Device | Name | Temperature (C) | RH (%) | Battery (V)")
    for row in rows:
        print(" | ".join("N/A" if value is None else str(value) for value in row))


def set_sensor_name(device, name):
    if not device.strip() or len(device) > 128 or not name.strip() or len(name) > 128:
        raise ValueError("Device ID and name must contain 1 to 128 characters")
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("INSERT INTO sensors VALUES (?, ?) ON CONFLICT(device_id) "
                         "DO UPDATE SET name=excluded.name", (device, name.strip()))
    print(f"Sensor name saved: {device} = {name.strip()}")


class Receiver(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200, "HVAC receiver is running")

    def do_POST(self):
        # Receive -> validate -> atomically save reading/alert state -> acknowledge.
        if self.path != "/data":
            self.reply(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                self.reply(400, "Invalid message size")
                return
            data = json.loads(self.rfile.read(length))
            validate_reading(data)
        except (ValueError, UnicodeDecodeError, OverflowError) as exc:
            self.reply(400, f"Invalid data: {exc}")
            return
        except TimeoutError:
            self.reply(408, "Request body timed out")
            return

        try:
            row_id, timestamp = save_reading(data, self.client_address[0])
        except sqlite3.Error as exc:
            print(f"Database save failed: {exc}", flush=True)
            self.reply(500, "Database save failed; reading was not saved")
            return
        print(f"[{timestamp}] Saved #{row_id} from {self.client_address[0]}: {data}",
              flush=True)
        self.reply(200, "OK")


def main():
    global SLACK_ENABLED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent", type=int, metavar="N",
                        help="Show the latest N saved readings, then exit")
    parser.add_argument("--set-name", nargs=2, metavar=("DEVICE_ID", "NAME"))
    parser.add_argument("--device", help="Filter recent readings by stable device ID")
    parser.add_argument("--name", help="Filter recent readings by current sensor name")
    parser.add_argument("--start", help="Inclusive ISO time with timezone offset")
    parser.add_argument("--end", help="Exclusive ISO time with timezone offset")
    parser.add_argument("--test-slack", action="store_true",
                        help="Send one clearly labeled test message, then exit")
    args = parser.parse_args()
    if args.recent is not None and args.recent < 1:
        parser.error("--recent must be at least 1")
    try:
        if args.test_slack:
            if args.set_name or args.recent is not None or any((args.device, args.name, args.start, args.end)):
                raise ValueError("Use --test-slack separately from database commands")
            url = slack_url_from_environment()
            if not url:
                raise ValueError("Set HVAC_SLACK_WEBHOOK_URL in this terminal first")
            error = send_slack(url, "TEST — HVAC Slack connection test. No sensor alarm was triggered.")
            if error:
                parser.exit(1, error + "\n")
            print("Slack test sent successfully. Check the configured channel.")
            return
        start = normalized_time(args.start) if args.start else None
        end = normalized_time(args.end) if args.end else None
        if start and end and start >= end:
            raise ValueError("--start must be before --end")
        if args.set_name and args.recent is not None:
            raise ValueError("Use --set-name and --recent separately")
        if any((args.device, args.name, args.start, args.end)) and args.recent is None:
            raise ValueError("Query filters require --recent N")
        init_database()
        if args.set_name:
            set_sensor_name(*args.set_name)
            return
        if args.recent is not None:
            show_recent(args.recent, args.device, args.name, start, end)
            return
        url = slack_url_from_environment()
        SLACK_ENABLED = bool(url)
        server = ThreadingHTTPServer(("0.0.0.0", 8000), Receiver)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Startup failed: {exc}\n")
    print(f"Receiver started on port 8000. Database: {DB_PATH}", flush=True)
    stop = threading.Event()
    worker = None
    if url:
        worker = threading.Thread(target=slack_worker, args=(url, stop), daemon=True)
        worker.start()
        print("Slack enabled; queued notifications retry after failures.", flush=True)
    else:
        print("Slack disabled: HVAC_SLACK_WEBHOOK_URL is not set. New alerts will not be queued.", flush=True)
    print("Press Ctrl+C to stop. Saved data will remain on disk.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        if worker:
            worker.join(timeout=SLACK_TIMEOUT + 3)


if __name__ == "__main__":
    main()
