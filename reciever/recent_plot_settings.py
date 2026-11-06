"""Independent Recent Plots axis preferences; no alarm settings are changed."""
import json
import math
import sqlite3
from contextlib import closing

AXES = {'indoor_temp': 'Indoor temperature (°C)', 'indoor_rh': 'Indoor humidity (%RH)',
        'outdoor_temp': 'Outdoor temperature (°C)', 'outdoor_rh': 'Outdoor humidity (%RH)'}


def read(db):
    with closing(sqlite3.connect(db, timeout=.5)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='recent_plot_settings'").fetchone():
            return {'version': 0, 'bounds': {}}
        row = conn.execute('SELECT version, bounds FROM recent_plot_settings WHERE id=1').fetchone()
        return {'version': row[0], 'bounds': json.loads(row[1])} if row else {'version': 0, 'bounds': {}}


def validate(bounds):
    if set(bounds) - set(AXES):
        raise ValueError('Unknown axis.')
    for pair in bounds.values():
        if len(pair) != 2 or not all(math.isfinite(float(v)) for v in pair) or pair[0] >= pair[1]:
            raise ValueError('Each minimum must be finite and smaller than its maximum.')


def save(db, bounds, version, user_id, editors):
    if user_id not in editors:
        raise PermissionError('You do not have permission to change plot ranges.')
    validate(bounds)
    with closing(sqlite3.connect(db, timeout=.5)) as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS recent_plot_settings '
                         '(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, bounds TEXT NOT NULL, updated_by TEXT NOT NULL)')
            row = conn.execute('SELECT version FROM recent_plot_settings WHERE id=1').fetchone()
            current = row[0] if row else 0
            if version != current:
                raise ValueError('Settings changed. Close and reopen this form.')
            conn.execute('INSERT OR REPLACE INTO recent_plot_settings VALUES (1,?,?,?)',
                         (current+1, json.dumps(bounds), user_id))
