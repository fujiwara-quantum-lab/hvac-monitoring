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


def render_history(history, sensor_name, range_label, settings, gap_seconds=180):
    """Return separate high-resolution temperature and RH PNGs plus raw-sample stats.

    Both y axes zoom independently. Current alarm bounds are drawn only if they
    fall inside the zoomed axis, so broad thresholds cannot flatten small changes.
    The x axis covers actual observations within the chosen history window.
    """
    points = history["points"]
    if not points:
        return None
    temperatures, humidities = [p[1] for p in points], [p[2] for p in points]
    stats = {"temperature": sample_stats(temperatures), "humidity": sample_stats(humidities)}
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
                    markersize=4 if len(points) < 500 else 2, label="Raw readings", zorder=3)
            limits = axis_limits(values, min_span)
            for caption, field in (("Lower limit", low_key), ("Upper limit", high_key)):
                if limits[0] <= settings[field] <= limits[1]:
                    ax.axhline(settings[field], color="#b23a3a", linestyle="--", linewidth=1,
                               label=f"{caption}: {settings[field]:g}")
            ax.set_ylim(*limits)
            span = (points[-1][0] - points[0][0]).total_seconds()
            padding = timedelta(seconds=max(30, span * .025))
            ax.set_xlim(points[0][0]-padding, points[-1][0]+padding)
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
            summary = (f"{range_label} | Min {st['min']:.3f} | Max {st['max']:.3f} {unit}\n"
                       f"Peak-to-peak {st['ptp']:.3f} | Std {st['std']:.3f} {unit}")
            ax.set_title(summary, loc="left", fontsize=12, pad=10)
            ax.set_ylabel(unit, fontsize=12)
            ax.legend(loc="best", fontsize=10, framealpha=.9)
            footer = (f"{len(points):,} raw samples | UTC: {first} to {last}\n"
                      f"Auto-scaled axes; no averaging. Gaps > {gap_seconds:g} s not joined ({gap_count} gaps).\n"
                      f"Current limits {settings[low_key]:g} to {settings[high_key]:g} {unit}; off-scale limits omitted. "
                      "Std: population std.")
            if history["invalid"]:
                footer += f" Skipped invalid rows: {history['invalid']}."
            fig.text(.09, .025, footer, fontsize=8.5, color="#455a64", va="bottom")
            fig.subplots_adjust(left=.10, right=.98, top=.74, bottom=.25)
            buffer = BytesIO()
            fig.savefig(buffer, format="png", dpi=300, facecolor="white",
                        metadata={"Software": "HVAC Monitor"})
            panels.append({"key": key, "title": title, "png": buffer.getvalue()})
            fig.clear()
    return {"panels": panels, "stats": stats, "gaps": gap_count}
