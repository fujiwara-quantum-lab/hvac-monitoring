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
            conn.execute("""
                CREATE INDEX IF NOT EXISTS readings_device_time
                ON readings(device_id, received_at_utc)
            """)


def validate_reading(data):
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
            cursor = conn.execute("""
                INSERT INTO readings
                    (received_at_utc, device_id, temperature_c, humidity_rh,
                     battery_v, source_ip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (timestamp, data["device_id"], data["temperature_c"],
                  data["humidity_rh"], data.get("battery_v"), source_ip))
            row_id = cursor.lastrowid
    return row_id, timestamp


def show_recent(limit):
    if not DB_PATH.exists():
        print("No database yet. Run python server.py first.")
        return
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        rows = conn.execute("""
            SELECT id, received_at_utc, device_id, temperature_c,
                   humidity_rh, battery_v FROM readings
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    print(f"Database: {DB_PATH}\nTotal saved readings: {total}")
    print("ID | Received time (UTC) | Device | Temperature (C) | Humidity (%) | Battery (V)")
    for row in rows:
        print(" | ".join("N/A" if value is None else str(value) for value in row))


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
    args = parser.parse_args()
    if args.recent is not None and args.recent < 1:
        parser.error("--recent must be at least 1")
    try:
        if args.recent is not None:
            show_recent(args.recent)
            return
        init_database()
        server = ThreadingHTTPServer(("0.0.0.0", 8000), Receiver)
    except (OSError, sqlite3.Error) as exc:
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
