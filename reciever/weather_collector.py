"""Outdoor reference weather for HVAC monitoring (Python 3.10+, standard library).

Place beside server.py. Start server.py once to create data/hvac.db, then:
  python weather_collector.py --once
  python weather_collector.py
  python weather_collector.py --recent 10
Optional: --db PATH --station KABE --interval 60 --stale-minutes 120
Ctrl+C stops the collector. Keep server.py and slack_app.py running separately.

NWS KABE represents Lehigh Valley airport, not a sensor outside the laboratory.
A 60-second poll does not imply 60-second observations. Unique observations are
stored by source/station/observation time; repeats update last_fetched_at_utc.
Times are UTC; terminal output also shows PC local time. Missing values stay NULL.
No indoor tables, thresholds, or Slack alerts are modified. No historical backfill.
API documentation: https://www.weather.gov/documentation/services-web-api
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.request

SOURCE = 'NWS'
DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'hvac.db'


def utcnow():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def parse_time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Observation timestamp has no timezone')
    return dt.astimezone(timezone.utc)


def connect(path):
    # mode=rw avoids silently creating a second empty database at the wrong path.
    return sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)


def initialize(path):
    with closing(connect(path)) as c, c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='readings'").fetchone():
            raise ValueError('Not an HVAC database: readings table is missing')
        c.execute('''CREATE TABLE IF NOT EXISTS weather_observations (
            source TEXT NOT NULL, station_id TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL, first_fetched_at_utc TEXT NOT NULL,
            last_fetched_at_utc TEXT NOT NULL, temperature_c REAL,
            humidity_rh REAL, source_url TEXT NOT NULL, raw_json TEXT NOT NULL,
            PRIMARY KEY(source, station_id, observed_at_utc))''')
        c.execute('''CREATE TABLE IF NOT EXISTS weather_collector_status (
            source TEXT NOT NULL, station_id TEXT NOT NULL,
            last_attempt_at_utc TEXT NOT NULL, last_success_at_utc TEXT,
            last_error TEXT, PRIMARY KEY(source, station_id))''')


def quantity(p, key, unit):
    q = p.get(key)
    if not isinstance(q, dict) or q.get('value') is None:
        return None
    v = q['value']
    if q.get('unitCode') != unit:
        raise ValueError(f'Unexpected unit for {key}: {q.get("unitCode")}')
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f'Invalid {key}')
    if key == 'relativeHumidity' and not 0 <= v <= 100:
        raise ValueError('Relative humidity outside 0..100')
    return float(v)


def decode(payload, station, now):
    p = payload['properties']
    if p.get('station', '').rstrip('/').split('/')[-1] != station:
        raise ValueError('Response station does not match requested station')
    observed = parse_time(p['timestamp'])
    if (observed - now).total_seconds() > 300:
        raise ValueError('Observation is in the future; check the PC clock')
    return (stamp(observed), quantity(p, 'temperature', 'wmoUnit:degC'),
            quantity(p, 'relativeHumidity', 'wmoUnit:percent'))


def save(path, station, url, payload, now):
    observed, temp, rh = decode(payload, station, now)
    fetched = stamp(now)
    with closing(connect(path)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        exists = c.execute('SELECT 1 FROM weather_observations WHERE source=? AND station_id=? AND observed_at_utc=?',
                           (SOURCE, station, observed)).fetchone()
        c.execute('''INSERT INTO weather_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, station_id, observed_at_utc) DO UPDATE SET
            last_fetched_at_utc=excluded.last_fetched_at_utc,
            temperature_c=excluded.temperature_c, humidity_rh=excluded.humidity_rh,
            source_url=excluded.source_url, raw_json=excluded.raw_json''',
            (SOURCE, station, observed, fetched, fetched, temp, rh, url,
             json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        c.execute('''INSERT INTO weather_collector_status VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(source, station_id) DO UPDATE SET
            last_attempt_at_utc=excluded.last_attempt_at_utc,
            last_success_at_utc=excluded.last_success_at_utc, last_error=NULL''',
            (SOURCE, station, fetched, fetched))
    return observed, temp, rh, bool(exists)


def record_failure(path, station, error):
    with closing(connect(path)) as c, c:
        c.execute('''INSERT INTO weather_collector_status VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(source, station_id) DO UPDATE SET
            last_attempt_at_utc=excluded.last_attempt_at_utc, last_error=excluded.last_error''',
            (SOURCE, station, stamp(utcnow()), str(error)[:1000]))


def fetch(station, user_agent):
    url = f'https://api.weather.gov/stations/{station}/observations/latest'
    req = urllib.request.Request(url, headers={'User-Agent': user_agent, 'Accept': 'application/geo+json'})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.load(response)
    return url, payload


def describe(observed, temp, rh, now, stale_minutes):
    dt = parse_time(observed)
    age = max(0, (now - dt).total_seconds() / 60)
    flags = []
    if age > stale_minutes:
        flags.append('STALE')
    if temp is None or rh is None:
        flags.append('MISSING_VALUE')
    t = 'missing' if temp is None else f'{temp:.2f} C'
    h = 'missing' if rh is None else f'{rh:.2f} %RH'
    return f'{"/".join(flags) or "OK"} | {t} | {h} | observed {dt.astimezone().isoformat()} | age {age:.0f} min'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', type=Path, default=DEFAULT_DB)
    ap.add_argument('--station', type=str.upper, default='KABE')
    ap.add_argument('--interval', type=int, default=60)
    ap.add_argument('--stale-minutes', type=int, default=120)
    ap.add_argument('--user-agent', default='Fujiwara-Quantum-Lab-HVAC-Monitor/1.0')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--recent', type=int, metavar='N')
    args = ap.parse_args()
    if not re.fullmatch(r'[A-Z0-9]{3,8}', args.station):
        ap.error('Invalid station ID')
    if args.interval < 60 or args.stale_minutes < 1 or (args.recent is not None and args.recent < 1):
        ap.error('interval must be >=60; stale-minutes and recent must be positive')
    try:
        initialize(args.db)
    except (OSError, sqlite3.Error, ValueError) as e:
        ap.exit(1, f'Database error: {e}\nPlace this script beside server.py, start server.py first, or use --db PATH.\n')
    print(f'Database: {args.db.resolve()}\nSource: NWS / {args.station}', flush=True)
    if args.recent is not None:
        with closing(connect(args.db)) as c:
            rows = c.execute('''SELECT observed_at_utc, temperature_c, humidity_rh FROM weather_observations
                WHERE source=? AND station_id=? ORDER BY observed_at_utc DESC LIMIT ?''',
                (SOURCE, args.station, args.recent)).fetchall()
        for row in rows:
            print(describe(*row, utcnow(), args.stale_minutes))
        if not rows:
            print('No weather observations saved yet.')
        return 0
    failures = 0
    try:
        while True:
            start = time.monotonic()
            try:
                url, payload = fetch(args.station, args.user_agent)
                now = utcnow()
                observed, temp, rh, duplicate = save(args.db, args.station, url, payload, now)
                print(f'[{stamp(now)}] {"SEEN" if duplicate else "NEW"} | '
                      + describe(observed, temp, rh, now, args.stale_minutes), flush=True)
                failures = 0
                if args.once:
                    return 0
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as e:
                failures += 1
                print(f'[{stamp(utcnow())}] ERROR: {e}', flush=True)
                try:
                    record_failure(args.db, args.station, e)
                except sqlite3.Error as db_error:
                    print(f'Cannot save failure status: {db_error}', flush=True)
                if args.once:
                    return 1
            delay = min(args.interval * 2 ** min(max(0, failures - 1), 4), max(args.interval, 900))
            time.sleep(max(0, delay - (time.monotonic() - start)))
    except KeyboardInterrupt:
        print('\nCollector stopped.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
