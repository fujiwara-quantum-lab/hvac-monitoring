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
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# Inclusive comfort bounds. Physical validity is checked separately below.
TEMP_MIN, TEMP_MAX = 20.0, 26.0
RH_MIN, RH_MAX = 40.0, 60.0

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
            events = []
            for metric, low, high in (("temperature_c", TEMP_MIN, TEMP_MAX),
                                      ("humidity_rh", RH_MIN, RH_MAX)):
                value = data[metric]
                status = "low" if value < low else "high" if value > high else "normal"
                previous = conn.execute(
                    "SELECT status FROM alert_state WHERE device_id=? AND metric=?",
                    (data["device_id"], metric)).fetchone()
                # Initial normal readings are quiet; initial abnormal readings alert.
                if status != (previous[0] if previous else "normal"):
                    conn.execute("INSERT INTO alerts (reading_id, received_at_utc, "
                                 "device_id, metric, status, value) VALUES (?, ?, ?, ?, ?, ?)",
                                 (row_id, timestamp, data["device_id"], metric, status, value))
                    events.append(f"{metric}: {status} ({value}; bounds {low} to {high})")
                conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?, ?, ?)",
                             (data["device_id"], metric, status))
            name = conn.execute("SELECT name FROM sensors WHERE device_id=?",
                                (data["device_id"],)).fetchone()[0]
    # Log only after the entire transaction committed successfully.
    for event in events:
        print(f"[ALERT/RECOVERY] {timestamp} {name} ({data['device_id']}): {event}", flush=True)
    return row_id, timestamp


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent", type=int, metavar="N",
                        help="Show the latest N saved readings, then exit")
    parser.add_argument("--set-name", nargs=2, metavar=("DEVICE_ID", "NAME"))
    parser.add_argument("--device", help="Filter recent readings by stable device ID")
    parser.add_argument("--name", help="Filter recent readings by current sensor name")
    parser.add_argument("--start", help="Inclusive ISO time with timezone offset")
    parser.add_argument("--end", help="Exclusive ISO time with timezone offset")
    args = parser.parse_args()
    if args.recent is not None and args.recent < 1:
        parser.error("--recent must be at least 1")
    try:
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
        server = ThreadingHTTPServer(("0.0.0.0", 8000), Receiver)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Startup failed: {exc}\n")
    print(f"Receiver started on port 8000. Database: {DB_PATH}", flush=True)
    print("Press Ctrl+C to stop. Saved data will remain on disk.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
