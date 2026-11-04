"""Read-only HVAC history plotting. No alarm rules or database writes live here.

Matplotlib is used with a noninteractive backend; plotting is serialized because
Matplotlib is not thread-safe. Values/statistics use raw received samples, with
population standard deviation (ddof=0), not time-weighted statistics.
"""

from contextlib import closing
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
import math
import sqlite3
import statistics
import threading

PLOT_LOCK = threading.Lock()
MAX_ROWS = 200000


def load_history(db_path, device_id, seconds, now):
    """Return [start, end) rows, scoped to this sensor, without creating a DB."""
    end = now.astimezone(timezone.utc)
    start = end - timedelta(seconds=seconds)
    # Stored timestamps have millisecond precision. Broaden the SQL upper bound
    # by at most 1 ms, then enforce the exact half-open interval below in Python.
    sql_end = end + timedelta(microseconds=1000 - end.microsecond % 1000)
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as conn:
        rows = conn.execute(
            "SELECT id, received_at_utc, temperature_c, humidity_rh FROM readings "
            "WHERE device_id=? AND received_at_utc>=? AND received_at_utc<? "
            "ORDER BY received_at_utc, id LIMIT ?",
            (device_id, start.isoformat(timespec="milliseconds"),
             sql_end.isoformat(timespec="milliseconds"), MAX_ROWS + 1)).fetchall()
    if len(rows) > MAX_ROWS:
        raise ValueError("Too many samples for this plot. Select a shorter time window.")
    points, invalid = [], 0
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not (start <= stamp < end):
                raise ValueError
            temp, rh = float(row[2]), float(row[3])
            if not math.isfinite(temp) or not math.isfinite(rh):
                raise ValueError
            points.append((stamp.astimezone(timezone.utc), temp, rh))
        except (TypeError, ValueError, OverflowError):
            invalid += 1
    return {"start": start, "end": end, "points": points, "invalid": invalid}


def load_weather(db_path, start, end):
    """Read NWS/KABE by observation time; never substitute fetch time or fill gaps."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='weather_observations'").fetchone():
                return {"points": [], "note": "Outdoor history not available yet. Start weather_collector.py."}
            rows = conn.execute(
                "SELECT observed_at_utc, temperature_c, humidity_rh FROM weather_observations "
                "WHERE source='NWS' AND station_id='KABE' AND observed_at_utc>=? AND observed_at_utc<? "
                "ORDER BY observed_at_utc LIMIT ?",
                (start.isoformat(timespec="milliseconds"),
                 (end + timedelta(milliseconds=1)).isoformat(timespec="milliseconds"), MAX_ROWS+1)).fetchall()
        if len(rows) > MAX_ROWS:
            return {"points": [], "note": "Too many outdoor observations; choose a shorter window."}
        points = []
        for ts, t, rh in rows:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None or not start <= dt < end:
                    continue
                vals = []
                for value in (t, rh):
                    v = float(value) if value is not None else float("nan")
                    vals.append(v if math.isfinite(v) else float("nan"))
                points.append((dt.astimezone(timezone.utc), *vals))
            except (TypeError, ValueError, OverflowError):
                continue
        note = (f"Outdoor reference: NWS / KABE | {len(points)} observations in this window. "
                "Observation times; not minute-by-minute measurements.") if points else "No outdoor observations in this time window."
        if points:
            age = (end-points[-1][0]).total_seconds()/60
            if age > 120:
                note += f" Last observation is {age:.0f} min before the window end."
            if any(not math.isfinite(p[i]) for p in points for i in (1, 2)):
                note += " Some outdoor values are missing."
        return {"points": points, "note": note}
    except sqlite3.Error:
        return {"points": [], "note": "Outdoor history temporarily unavailable; indoor history shown."}


def axis_limits(values, minimum_span):
    """Zoom around data, never force zero or distant alarm bounds into view."""
    low, high = min(values), max(values)
    span = max(high - low, minimum_span)
    middle = (low + high) / 2
    padding = span * 0.15
    return middle - span/2 - padding, middle + span/2 + padding


def sample_stats(values):
    return {"n": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values), "ptp": max(values)-min(values),
            "std": statistics.pstdev(values)}


def plot_series(points, metric, gap_seconds):
    """Insert NaN between distant readings; empty periods are never zero-filled."""
    xs, ys, previous = [], [], None
    gaps = 0
    for point in points:
        if previous is not None and (point[0] - previous).total_seconds() > gap_seconds:
            xs.append(point[0]); ys.append(float("nan")); gaps += 1
        xs.append(point[0]); ys.append(point[metric]); previous = point[0]
    return xs, ys, gaps


def render_history(history, sensor_name, range_label, settings, gap_seconds=180, weather=None):
    """Return separate high-resolution temperature and RH PNGs plus raw-sample stats.

    Axis limits follow the data. Alarm thresholds are not drawn or annotated.
    The x axis covers actual observations within the chosen history window.
    """
    points = history["points"]
    if not points:
        return None
    temperatures, humidities = [p[1] for p in points], [p[2] for p in points]
    stats = {"temperature": sample_stats(temperatures), "humidity": sample_stats(humidities)}
    outdoor = (weather or {}).get("points", [])
    panels = []
    with PLOT_LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, DateFormatter
        from matplotlib.ticker import MaxNLocator, ScalarFormatter
        safe_name = " ".join(str(sensor_name).replace("$", "").split())[:65]
        first = points[0][0].strftime("%Y-%m-%d %H:%M:%S")
        last = points[-1][0].strftime("%Y-%m-%d %H:%M:%S")
        for key, metric, title, unit, color, min_span, low_key, high_key in (
                ("temperature", 1, "Temperature", "C", "#1565c0", .2, "temp_min", "temp_max"),
                ("humidity", 2, "Relative humidity", "%RH", "#00897b", 1, "rh_min", "rh_max")):
            # Separate figures keep each graph readable in Slack's narrow modal.
            # PNG dimensions are 3000 x 1560; Slack may still show a smaller preview.
            fig = Figure(figsize=(10, 5.2), dpi=300, facecolor="white")
            FigureCanvasAgg(fig)
            ax = fig.subplots()
            values = temperatures if metric == 1 else humidities
            xs, ys, gap_count = plot_series(points, metric, gap_seconds)
            ax.plot(xs, ys, color=color, linewidth=1.4, marker=".",
                    markersize=4 if len(points) < 500 else 2, label="Indoor sensor", zorder=3)
            outdoor_values = [p[metric] for p in outdoor if math.isfinite(p[metric])]
            if outdoor_values:
                wx, wy, _ = plot_series(outdoor, metric, 7200)
                ax.plot(wx, wy, color="#e87916", linewidth=1.3, linestyle="--",
                        marker="o", markersize=4, label="Outdoor: NWS / KABE", zorder=2)
            limits = axis_limits(values + outdoor_values, min_span)
            ax.set_ylim(*limits)
            span = (points[-1][0] - points[0][0]).total_seconds()
            padding = timedelta(seconds=max(30, span * .025))
            ax.set_xlim(history["start"], history["end"])
            locator = AutoDateLocator(minticks=4, maxticks=6, tz=timezone.utc)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(DateFormatter("%m-%d\n%H:%M", tz=timezone.utc))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
            formatter = ScalarFormatter(useOffset=False)
            formatter.set_scientific(False)
            ax.yaxis.set_major_formatter(formatter)
            ax.tick_params(axis="both", labelsize=12)
            ax.grid(True, color="#dce2e8", linewidth=.7, zorder=0)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            st = stats[key]
            fig.suptitle(f"{safe_name} | {title}", fontsize=16, fontweight="bold", y=.97)
            summary = (f"{range_label} | Indoor min {st['min']:.3f} | Max {st['max']:.3f} {unit}\n"
                       f"Peak-to-peak {st['ptp']:.3f} | Std {st['std']:.3f} {unit}")
            ax.set_title(summary, loc="left", fontsize=12, pad=10)
            ax.set_ylabel(unit, fontsize=12)
            ax.legend(loc="best", fontsize=10, framealpha=.9)
            footer = (f"{len(points):,} raw samples | UTC: {first} to {last}\n"
                      f"Auto-scaled axes; no averaging. Gaps > {gap_seconds:g} s not joined ({gap_count} gaps).\n"
                      "Std: population std.")
            if weather is not None:
                footer += "\nOutdoor: NWS/KABE airport reference; gaps > 2 h not joined. "
                footer += f"{len(outdoor_values)} valid observations." if outdoor_values else "No valid outdoor data in window."
            if history["invalid"]:
                footer += f" Skipped invalid rows: {history['invalid']}."
            fig.text(.09, .025, footer, fontsize=8.5, color="#455a64", va="bottom")
            fig.subplots_adjust(left=.10, right=.98, top=.74, bottom=.29)
            buffer = BytesIO()
            fig.savefig(buffer, format="png", dpi=300, facecolor="white",
                        metadata={"Software": "HVAC Monitor"})
            panels.append({"key": key, "title": title, "png": buffer.getvalue()})
            fig.clear()
    return {"panels": panels, "stats": stats, "gaps": gap_count}


RECENT_RANGES = {"6h": 21600, "72h": 259200, "7d": 604800, "30d": 2592000}


def load_recent(db_path, now=None):
    """Read all indoor sensors once for four windows, without database writes."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    end = now.astimezone(timezone.utc)
    start = end - timedelta(seconds=max(RECENT_RANGES.values()))
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.execute("BEGIN")
        sensors = conn.execute("SELECT device_id, name FROM sensors ORDER BY device_id").fetchall()
        rows = conn.execute(
            "SELECT device_id, received_at_utc, temperature_c, humidity_rh FROM readings "
            "WHERE received_at_utc>=? AND received_at_utc<? ORDER BY received_at_utc, id LIMIT ?",
            (start.isoformat(timespec="milliseconds"),
             (end+timedelta(milliseconds=1)).isoformat(timespec="milliseconds"),
             MAX_ROWS*10+1)).fetchall()
    if len(rows) > MAX_ROWS*10:
        raise ValueError("Recent plots exceed 2 million samples; reduce the history window.")
    grouped = {device: {"device_id": device, "name": name, "points": []} for device, name in sensors}
    invalid = 0
    for device, ts, temp, rh in rows:
        try:
            stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            temp, rh = float(temp), float(rh)
            if stamp.tzinfo is None or not math.isfinite(temp) or not math.isfinite(rh):
                raise ValueError
            if not start <= stamp < end:
                continue
            if device in grouped:
                grouped[device]["points"].append((stamp, temp, rh))
        except (ValueError, TypeError, OverflowError):
            invalid += 1
    return {"end": end, "sensors": list(grouped.values()),
            "weather": load_weather(db_path, start, end), "invalid": invalid}


def render_recent(snapshot, range_key, gap_seconds=180):
    """One PNG: aligned temperature/RH panels, independent outdoor right axes."""
    if range_key not in RECENT_RANGES:
        raise ValueError("Unknown recent plot range")
    end = snapshot["end"]
    start = end - timedelta(seconds=RECENT_RANGES[range_key])
    sensors = snapshot["sensors"]
    outdoor = [p for p in snapshot["weather"]["points"] if start <= p[0] < end]
    with PLOT_LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, DateFormatter
        from matplotlib.lines import Line2D
        from matplotlib.ticker import MaxNLocator, ScalarFormatter
        import hashlib
        palette = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00",
                   "#56B4E9", "#882255", "#44AA99", "#332288", "#999933"]
        # Resolve palette collisions consistently across all four windows.
        colors, used = {}, set()
        for sensor in sorted(sensors, key=lambda s: s["device_id"]):
            device = sensor["device_id"]
            index = int(hashlib.sha256(device.encode()).hexdigest()[:8], 16) % len(palette)
            if len(used) < len(palette):
                while index in used:
                    index = (index+1) % len(palette)
            used.add(index)
            colors[device] = palette[index]
        def color(device):
            return colors[device]
        legend_rows = math.ceil((len(sensors)+1)/3)
        legend_height = .65*legend_rows + .25
        height = 6.8 + legend_height
        fig = Figure(figsize=(12, height), dpi=180, facecolor="white")
        FigureCanvasAgg(fig)
        axes = fig.subplots(2, 1, sharex=True)
        fig.subplots_adjust(left=.09, right=.90, top=.97,
                            bottom=(legend_height+.65)/height, hspace=.12)
        handles = []
        for sensor in sensors:
            name = " ".join(str(sensor["name"]).replace("$", "").split())
            if sum(s["name"] == sensor["name"] for s in sensors) > 1:
                name += " [" + sensor["device_id"][-8:] + "]"
            points = [p for p in sensor["points"] if start <= p[0] < end]
            last = sensor["points"][-1][0] if sensor["points"] else None
            if not points:
                name += " (no data)"
            elif last is not None and (end-last).total_seconds() > gap_seconds:
                name += " (stale)"
            c = color(sensor["device_id"])
            handles.append(Line2D([], [], color=c, linewidth=1.5, label=name))
            for metric, ax in enumerate(axes, 1):
                xs, ys, _ = plot_series(points, metric, gap_seconds)
                ax.plot(xs, ys, color=c, linewidth=1, marker=".", markersize=1.5)
        handles.append(Line2D([], [], color="#333333", linestyle="--", marker="o",
                              markersize=3, label="Outdoor — NWS / KABE\n(right axis)"))
        for metric, (ax, label, unit, span) in enumerate(zip(
                axes, ("Temperature", "Humidity"), ("°C", "%RH"), (.2, 1)), 1):
            right = ax.twinx()
            wx, wy, _ = plot_series(outdoor, metric, 7200)
            right.plot(wx, wy, color="#333333", linestyle="--", linewidth=1.2,
                       marker="o", markersize=3)
            inside = [p[metric] for s in sensors for p in s["points"] if start <= p[0] < end]
            outside = [p[metric] for p in outdoor if math.isfinite(p[metric])]
            if inside: ax.set_ylim(*axis_limits(inside, span))
            else: ax.text(.02, .92, "No indoor data", transform=ax.transAxes, va="top", fontsize=10)
            if outside: right.set_ylim(*axis_limits(outside, span))
            else: ax.text(.98, .92, "No outdoor data", transform=ax.transAxes, ha="right", va="top", fontsize=10)
            ax.set_ylabel(f"Indoor {label} ({unit})", fontsize=11)
            right.set_ylabel(f"Outdoor {label} ({unit})", fontsize=11)
            ax.set_xlim(start, end)
            ax.grid(True, color="#dce2e8", linewidth=.6)
            for axis in (ax, right):
                axis.yaxis.set_major_locator(MaxNLocator(nbins=5))
                formatter = ScalarFormatter(useOffset=False)
                formatter.set_scientific(False)
                axis.yaxis.set_major_formatter(formatter)
                axis.spines['top'].set_visible(False)
        axes[-1].xaxis.set_major_locator(AutoDateLocator(minticks=4, maxticks=7, tz=timezone.utc))
        axes[-1].xaxis.set_major_formatter(DateFormatter("%m-%d\n%H:%M", tz=timezone.utc))
        axes[-1].set_xlabel("Time (UTC)")
        fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(.5,.01),
                   ncol=3, prop={"size": 14, "weight": "bold"}, frameon=False,
                   handlelength=2, columnspacing=1.2, labelspacing=.7)
        buffer = BytesIO()
        fig.savefig(buffer, format="png", dpi=180, facecolor="white")
        fig.clear()
    return buffer.getvalue()


def export_recent(db_path, output_dir, now=None):
    """Build all four images from one snapshot; return paths and data cutoff."""
    snapshot = load_recent(db_path, now)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key in RECENT_RANGES:
        path = destination / f"recent_{key}.png"
        path.write_bytes(render_recent(snapshot, key))
        paths[key] = str(path)
    return {"paths": paths, "data_cutoff_utc": snapshot["end"].isoformat(),
            "weather_note": snapshot["weather"]["note"], "invalid_rows": snapshot["invalid"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate four Recent Plots PNGs from the HVAC database.")
    parser.add_argument("--recent", action="store_true", required=True)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent / "data" / "hvac.db"))
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "recent_plots"))
    args = parser.parse_args()
    result = export_recent(args.db, args.output)
    for key, path in result["paths"].items():
        print(f"{key}: {path}")
    print("Data cutoff (UTC):", result["data_cutoff_utc"])
    print(result["weather_note"])
