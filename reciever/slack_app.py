"""HVAC Slack App: Python temperature/RH images with zoomed axes and unchanged alert settings.

Install once: python -m pip install slack-bolt matplotlib
Run: python slack_app.py
Windows user environment variables:
    SLACK_BOT_TOKEN: xoxb-... (OAuth & Permissions, bot token)
    SLACK_APP_TOKEN: xapp-... (Basic Information, connections:write)
The two tokens must belong to the SAME Slack app.

Place beside the UPDATED server.py and run both programs. They share data/hvac.db.
The panel reads SQLite without writing measurements or sending alerts.
Only the explicit settings form changes settings. HVAC_DATA_STALE_SECONDS optionally
sets the panel's age warning (default 180); it is not an offline alarm or sleep setting.
"""

import os
import logging
import json
import sqlite3
import math
import hashlib
import re
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import threading
from collections import OrderedDict
from io import BytesIO
import hvac_plot
from contextlib import closing
from pathlib import Path

import server as receiver

if Path(receiver.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise RuntimeError("Place slack_app.py beside this project's server.py.")
from datetime import datetime, timezone, timedelta


def plain(text):
    return {"type": "plain_text", "text": text}


def markdown(text):
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def safe_label(text):
    # Sensor names are user data, never Slack mentions or formatting instructions.
    text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.translate(str.maketrans({"*": "＊", "_": "＿", "~": "～", "`": "ˋ"})).replace("\n", " ")


def freshness(reading, now):
    if reading is None:
        return "NO DATA"
    try:
        received = datetime.fromisoformat(reading["received_at_utc"].replace("Z", "+00:00"))
        if received.tzinfo is None:
            return "UNKNOWN AGE"
        age = (now - received).total_seconds()
        if age < -5:
            return "CLOCK MISMATCH"
        return "STALE" if age > stale_seconds() else "RECENT"
    except (ValueError, TypeError, OverflowError):
        return "UNKNOWN AGE"


def sensor_overview(settings, rows, now):
    # Chunk every row, not just the first page. Slack sections allow 3000 chars.
    blocks = [{"type": "header", "text": plain("All sensors")}]
    lines = []
    for row in rows:
        age = freshness(row if row["received_at_utc"] else None, now)
        issues = []
        if row["received_at_utc"]:
            for value, low, high, label in (
                (row["temperature_c"], settings["temp_min"], settings["temp_max"], "Temperature"),
                (row["humidity_rh"], settings["rh_min"], settings["rh_max"], "RH")):
                state = condition(value, low, high)
                if state != "NORMAL":
                    issues.append(label + (" low" if state == "LOW" else " high"))
        status = ", ".join(issues) if issues else "Normal"
        if age != "RECENT":
            status = age.title() + (" · Last: " + status if row["received_at_utc"] else "")
        icon = "⏳" if age != "RECENT" else ("⚠️" if issues else "✅")
        battery = "—" if row["battery_v"] is None else f"{row['battery_v']:.3f} V"
        # Keep the entire sensor name without exceeding Slack's section limit.
        line = f"*{safe_label(row['name'])[:500]}* · {icon} {status} · 🔋 {battery}"
        if sum(len(x)+1 for x in lines) + len(line) > 2800:
            blocks.append({"type": "section", "text": markdown("\n".join(lines))})
            lines = []
        lines.append(line)
    blocks.append({"type": "section", "text": markdown("\n".join(lines) or "No sensors registered yet.")})
    return blocks


def stale_seconds():
    """Presentation-only freshness cutoff, independent of temperature/RH alarms."""
    raw = os.environ.get("HVAC_DATA_STALE_SECONDS", "180")
    try:
        seconds = float(raw)
        if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ValueError
    except ValueError:
        raise ValueError("HVAC_DATA_STALE_SECONDS must be a number from 1 to 604800.") from None
    return seconds


def readonly_database():
    # mode=ro prevents a misplaced app from silently creating an empty database.
    uri = Path(receiver.DB_PATH).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=0.5)


def sensor_option(device_id, name):
    # A stable device ID is the option value, so duplicate names remain distinct.
    # Slack limits option labels to 75 characters and values to 150.
    label = f"{name[:47]} | {device_id.replace(':', '-')}"
    if len(label) > 75:
        label = label[:72] + "..."
    return {"text": plain(label), "value": device_id}


def load_sensor_options(search=""):
    # instr() treats % and _ literally, unlike LIKE wildcards.
    with closing(readonly_database()) as conn:
        rows = conn.execute(
            "SELECT device_id, name FROM sensors WHERE "
            "instr(lower(name), lower(?)) > 0 OR instr(lower(device_id), lower(?)) > 0 "
            "ORDER BY name COLLATE NOCASE, device_id LIMIT 100",
            (search, search)).fetchall()
    return [sensor_option(device, name) for device, name in rows]


def sensor_snapshot(device_id=None):
    """Read identity, thresholds and latest received sample in one DB snapshot."""
    with closing(readonly_database()) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("BEGIN")
            settings = receiver.read_settings(conn)
            count = conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0]
            if device_id:
                sensor = conn.execute("SELECT device_id, name FROM sensors WHERE device_id=?",
                                      (device_id,)).fetchone()
            else:
                sensor = conn.execute("SELECT device_id, name FROM sensors "
                                      "ORDER BY name COLLATE NOCASE, device_id LIMIT 1").fetchone()
            reading = None
            if sensor:
                reading = conn.execute(
                    "SELECT id, received_at_utc, temperature_c, humidity_rh, battery_v "
                    "FROM readings WHERE device_id=? "
                    "ORDER BY received_at_utc DESC, id DESC LIMIT 1",
                    (sensor["device_id"],)).fetchone()
            overview = [dict(row) for row in conn.execute(
                "SELECT s.device_id, s.name, r.received_at_utc, r.temperature_c, r.humidity_rh, r.battery_v "
                "FROM sensors s LEFT JOIN readings r ON r.id = ("
                "SELECT id FROM readings WHERE device_id=s.device_id "
                "ORDER BY received_at_utc DESC, id DESC LIMIT 1) "
                "ORDER BY s.name COLLATE NOCASE, s.device_id")]
    return settings, dict(sensor) if sensor else None, dict(reading) if reading else None, count, overview


def condition(value, low, high):
    if value < low:
        return "LOW"
    if value > high:
        return "HIGH"
    return "NORMAL"


def format_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} sec"
    if seconds < 86400:
        return f"{seconds // 3600} hr {(seconds % 3600) // 60} min"
    return f"{seconds // 86400} days {(seconds % 86400) // 3600} hr"


def latest_reading_text(reading, settings, now):
    if reading is None:
        return "NO READINGS YET. This sensor has a name but has not uploaded a reading."
    temp, rh = reading["temperature_c"], reading["humidity_rh"]
    battery = reading["battery_v"]
    lines = [
        f"Temperature: {temp:.2f} C — {condition(temp, settings['temp_min'], settings['temp_max'])}",
        f"Humidity: {rh:.2f} %RH — {condition(rh, settings['rh_min'], settings['rh_max'])}",
        "Battery voltage: " + ("N/A" if battery is None else f"{battery:.3f} V"),
        f"Last received (UTC): {reading['received_at_utc']}",
    ]
    try:
        received = datetime.fromisoformat(reading["received_at_utc"].replace("Z", "+00:00"))
        if received.tzinfo is None:
            raise ValueError("Timestamp missing timezone")
        age = (now - received).total_seconds()
        cutoff = stale_seconds()
        if age < -5:
            lines.append("Data freshness: CLOCK MISMATCH — timestamp is ahead of this computer's clock.")
        else:
            freshness = "STALE" if age > cutoff else "RECENT"
            lines.append(f"Data age: {format_age(age)} — {freshness}")
    except (ValueError, TypeError, OverflowError):
        lines.append("Data freshness: UNKNOWN — check timestamp format / freshness configuration.")
    return "\n".join(lines)


HISTORY_RANGES = {"1h": ("Last 1 hour", 3600), "24h": ("Last 24 hours", 86400),
                  "7d": ("Last 7 days", 604800)}
# Cache already-uploaded images in this process. Identical raw samples/settings
# reuse the same Slack file. Cached files remain available to older panels.
IMAGE_CACHE = OrderedDict()
READY_PLOT_FILES = set()
PLOT_FILE_LINKS = {}
UPLOAD_LOCK = threading.Lock()


def gap_seconds():
    raw = os.environ.get("HVAC_PLOT_GAP_SECONDS", str(stale_seconds()))
    try:
        value = float(raw)
        if not math.isfinite(value) or not 1 <= value <= 604800:
            raise ValueError
    except ValueError:
        raise ValueError("HVAC_PLOT_GAP_SECONDS must be from 1 to 604800 seconds.") from None
    return value


def upload_plot(client, png, title):
    # Private upload: no channel post per refresh and no publicly hosted URL.
    # The same bot that uploads the image also owns the /hvac response.
    digest = hashlib.sha256(png).hexdigest()
    with UPLOAD_LOCK:
        if digest in IMAGE_CACHE:
            IMAGE_CACHE.move_to_end(digest)
            return IMAGE_CACHE[digest]
        response = client.files_upload_v2(file=BytesIO(png), filename="hvac-history.png",
                                         title=title, request_file_info=False)
        files = response.get("files") or []
        if not files and response.get("file"):
            files = [response["file"]]
        if not files or not files[0].get("id"):
            raise ValueError("Slack did not return an uploaded image ID.")
        file_id = files[0]["id"]
        IMAGE_CACHE[digest] = file_id
        while len(IMAGE_CACHE) > 32:
            _, old_id = IMAGE_CACHE.popitem(last=False)
            READY_PLOT_FILES.discard(old_id)
            PLOT_FILE_LINKS.pop(old_id, None)
        return file_id


class PlotNotReady(ValueError):
    pass


def prepare_plot_files(client, file_ids):
    """Check actual Slack thumbnail metadata BEFORE offering the View button.

    Requires files:read. Preview metadata is a stronger server-side readiness
    check than a fixed delay, but cannot certify rendering on the user's client.
    """
    pending = set(file_ids) - READY_PLOT_FILES
    for delay in (0, 1, 2, 3, 5, 8):
        if not pending:
            return
        if delay:
            time.sleep(delay)
        for file_id in tuple(pending):
            info = client.files_info(file=file_id).get("file", {})
            thumbnail = any(info.get(key) for key in
                            ("thumb_160", "thumb_360", "thumb_480", "thumb_720", "thumb_800", "thumb_960", "thumb_1024"))
            if info.get("mimetype", "").startswith("image/") and thumbnail:
                READY_PLOT_FILES.add(file_id)
                link = info.get("permalink")
                if isinstance(link, str) and link.startswith("https://"):
                    PLOT_FILE_LINKS[file_id] = link
                pending.remove(file_id)
    if pending:
        raise PlotNotReady("Slack is still preparing the images. Click Refresh shortly.")


def history_blocks(device_id, range_key, now, settings, client=None, sensor_name="Sensor",
                   custom_start=None, custom_end=None, custom_zone="America/New_York"):
    if range_key == "custom":
        start, end = parse_custom_bounds(custom_start, custom_end)
        label, duration = "Custom range", (end-start).total_seconds()
        now = end
        zone = ZoneInfo(custom_zone)
        heading = (f"History: {start.astimezone(zone).strftime('%Y-%m-%d %H:%M %Z')} to "
                   f"{end.astimezone(zone).strftime('%Y-%m-%d %H:%M %Z')}")
    else:
        label, duration = HISTORY_RANGES[range_key]
        heading = f"History: {label}"
    try:
        history = hvac_plot.load_history(receiver.DB_PATH, device_id, duration, now)
    except ValueError as exc:
        return [{"type": "section", "text": plain(str(exc))}]
    if not history["points"]:
        return [{"type": "section", "text": plain(heading + "\nNo readings in this window.")}]
    try:
        weather = hvac_plot.load_weather(receiver.DB_PATH, history["start"], history["end"])
        result = hvac_plot.render_history(history, sensor_name, label, settings, gap_seconds(), weather=weather)
        t, rh = result["stats"]["temperature"], result["stats"]["humidity"]
        heading += (f" | {t['n']} raw samples\n"
                    f"Peak-to-peak: temperature {t['ptp']:.3f} C; humidity {rh['ptp']:.3f} %RH")
        heading += "\n" + weather["note"]
        if client is None:
            return [{"type": "section", "text": plain(heading + "\nConnect the Slack app to display the image.")}]
        blocks = [{"type": "section", "text": plain(heading)}]
        for plot in result["panels"]:
            file_id = upload_plot(client, plot["png"], f"{sensor_name[:70]} - {plot['title']} - {label}")
            blocks.append({"type": "image", "slack_file": {"id": file_id},
                           "title": plain(plot["title"]),
                           "alt_text": f"{sensor_name[:100]} {plot['title']} indoor history with NWS/KABE outdoor reference."})
        prepare_plot_files(client, [b["slack_file"]["id"] for b in blocks if b.get("type") == "image"])
        return blocks
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("error") if response is not None else None
        if code == "missing_scope":
            message = "Add files:read and files:write under Bot Token Scopes, reinstall the app, and retry."
        elif isinstance(exc, PlotNotReady):
            message = str(exc)
        elif isinstance(exc, ImportError):
            message = "Install plotting support: python -m pip install matplotlib"
        elif code:
            message = f"Slack upload failed ({code}). Check the app permissions and retry."
        else:
            message = "Plot generation/upload failed. Check dependencies and network, then retry."
        # Never print raw exceptions, tokens or upload URLs.
        print(f"Plot unavailable ({type(exc).__name__}): {message}", flush=True)
        return [{"type": "section", "text": plain(heading + "\n" + message)}]


def range_selector(range_key):
    options = [{"text": plain(label), "value": key}
               for key, (label, _) in HISTORY_RANGES.items()]
    options.append({"text": plain("Custom dates..."), "value": "custom"})
    return {"type": "static_select", "action_id": "hvac_range_select",
            "placeholder": plain("History window"), "options": options,
            "initial_option": next(item for item in options if item["value"] == range_key)}


# Context is attached to both selector block IDs. Ephemeral interactions may
# omit message.blocks and companion state; never depend on those for identity.
SELECTOR_CONTEXTS = OrderedDict()
SELECTOR_CONTEXT_LOCK = threading.Lock()


def remember_selector_context(context):
    token = uuid.uuid4().hex
    with SELECTOR_CONTEXT_LOCK:
        SELECTOR_CONTEXTS[token] = dict(context)
        while len(SELECTOR_CONTEXTS) > 4096:
            SELECTOR_CONTEXTS.popitem(last=False)
    return token


def selector_context(body):
    for action in body.get("actions", []):
        block_id = action.get("block_id", "")
        if block_id.startswith("hvac_choice:"):
            token = block_id.split(":")[1]
            with SELECTOR_CONTEXT_LOCK:
                saved = SELECTOR_CONTEXTS.get(token)
                if saved is not None:
                    return dict(saved)
            raise ValueError("Panel expired. Run /hvac again to open a new panel.")
    return None


def connection_panel(user_id=None, device_id=None, range_key="1h", client=None,
                     custom_start=None, custom_end=None, custom_zone="America/New_York",
                     defer_history=False, panel_id=None):
    if range_key == "custom":
        parse_custom_bounds(custom_start, custom_end)
    if range_key not in (*HISTORY_RANGES, "custom"):
        range_key = "1h"
    settings, sensor, reading, count, overview = sensor_snapshot(device_id)
    now = datetime.now(timezone.utc)
    selected = sensor["device_id"] if sensor else (device_id or "")
    context = {"device_id": selected, "range": range_key, "custom_start": custom_start,
               "custom_end": custom_end, "custom_zone": custom_zone,
               "panel_id": panel_id or uuid.uuid4().hex}
    selector_token = remember_selector_context(context)
    buttons = [{"type": "button", "action_id": "hvac_test_refresh",
                "text": plain("Refresh"), "value": json.dumps(context)}]
    if range_key == "custom":
        buttons.append({"type": "button", "action_id": "hvac_custom_edit",
                        "text": plain("Edit time range"), "value": json.dumps(context)})
    if user_id in receiver.SETTINGS_EDITORS:
        buttons.append({"type": "button", "action_id": "hvac_settings_open",
                        "text": plain("Set alert limits"), "value": "settings"})
    blocks = sensor_overview(settings, overview, now)
    blocks.extend([{"type": "divider"}, {"type": "header", "text": plain("Selected sensor")}])
    if count:
        select = {"type": "external_select", "action_id": "hvac_sensor_select",
                  "min_query_length": 0, "placeholder": plain("Select or search sensor")}
        if sensor:
            select["initial_option"] = sensor_option(sensor["device_id"], sensor["name"])
        blocks.append({"type": "actions", "block_id": f"hvac_choice:{selector_token}:sensor", "elements": [select]})
    if sensor:
        blocks.append({"type": "section", "text": markdown(f"*{safe_label(sensor['name'])[:500]}*")})
        if reading:
            blocks.append({"type": "section", "fields": [
                markdown(f"*Temperature*\n*{reading['temperature_c']:.2f} °C* · " +
                         condition(reading["temperature_c"], settings["temp_min"], settings["temp_max"])),
                markdown(f"*Relative humidity*\n*{reading['humidity_rh']:.2f}%* · " +
                         condition(reading["humidity_rh"], settings["rh_min"], settings["rh_max"]))]})
            battery = "—" if reading["battery_v"] is None else f"{reading['battery_v']:.3f} V"
            blocks.append({"type": "context", "elements": [plain(
                f"{freshness(reading, now)} | Received (UTC): {reading['received_at_utc']} | Battery: {battery}")]})
        else:
            blocks.append({"type": "section", "text": plain("No readings yet.")})
        blocks.append({"type": "context", "elements": [plain("Device: " + sensor["device_id"].replace(":", "-"))]})
    else:
        blocks.append({"type": "section", "text": plain(
            "The selected sensor no longer exists. Select another sensor." if device_id else
            "No sensors registered yet. Wait for an upload, then click Refresh.")})
    blocks.extend([{"type": "divider"}, {"type": "section", "text": markdown("*History plots*")},
                   {"type": "actions", "block_id": f"hvac_choice:{selector_token}:range", "elements": [range_selector(range_key)]}])
    if range_key == "custom":
        start, end = parse_custom_bounds(custom_start, custom_end)
        zone = ZoneInfo(custom_zone)
        range_label = f"{start.astimezone(zone):%Y-%m-%d %H:%M %Z} → {end.astimezone(zone):%Y-%m-%d %H:%M %Z}"
    else:
        range_label = HISTORY_RANGES[range_key][0]
    blocks.append({"type": "context", "elements": [plain(range_label)]})
    history_request = None
    if sensor:
        args = (selected, range_key, now, settings, client, sensor["name"],
                custom_start, custom_end, custom_zone)
        if defer_history:
            blocks.append({"type": "section", "block_id": "hvac_history_pending",
                           "text": plain(f"Loading plots — {sensor['name'][:500]}…")})
            history_request = args
        else:
            blocks.extend(history_blocks(*args))
    blocks.extend([
        {"type": "divider"},
        {"type": "context", "elements": [markdown(
            f"*Alert limits · all sensors*  |  Temperature {settings['temp_min']:g}–{settings['temp_max']:g} °C"
            f"  |  RH {settings['rh_min']:g}–{settings['rh_max']:g}%")]},
        {"type": "context", "elements": [plain(
            f"Refreshed (UTC): {now:%Y-%m-%d %H:%M:%S}" +
            ("" if user_id in receiver.SETTINGS_EDITORS else " | View only"))]},
        {"type": "actions", "elements": buttons},
    ])
    return {"response_type": "ephemeral", "text": "HVAC Monitor - Latest stored sensor status",
            "blocks": blocks, "_history_request": history_request}


def panel_or_error(user_id, device_id=None, range_key="1h", client=None,
                   custom_start=None, custom_end=None, custom_zone="America/New_York",
                   defer_history=False, panel_id=None):
    try:
        return connection_panel(user_id, device_id, range_key, client, custom_start, custom_end,
                                custom_zone, defer_history, panel_id)
    except (sqlite3.Error, ValueError):
        return {"response_type": "ephemeral", "text": "HVAC database unavailable.", "blocks": [
            {"type": "section", "text": plain("Database busy/unavailable. Check server.py, then retry.")},
            {"type": "actions", "elements": [{"type": "button", "action_id": "hvac_test_refresh",
             "text": plain("Retry"), "value": json.dumps({"device_id": device_id or "", "range": range_key,
                "custom_start": custom_start, "custom_end": custom_end, "custom_zone": custom_zone,
                "panel_id": panel_id or uuid.uuid4().hex})}]}]}


# Each panel has its own generation and publication lock. Slow plotting never
# holds this lock. Checking AND sending under the lock prevents stale results
# from passing a check just before a newer request publishes its loading state.
PANEL_REQUESTS = OrderedDict()
PANEL_REQUEST_LOCK = threading.Lock()


def begin_panel_request(context, order=None):
    key = context.setdefault("panel_id", None) or uuid.uuid4().hex
    context["panel_id"] = key
    with PANEL_REQUEST_LOCK:
        state = PANEL_REQUESTS.get(key)
        if state is None:
            state = {"lock": threading.Lock(), "token": None, "order": -1}
            PANEL_REQUESTS[key] = state
        PANEL_REQUESTS.move_to_end(key)
        # Do not evict running requests: they still need their generation guard.
        for old in list(PANEL_REQUESTS):
            if len(PANEL_REQUESTS) <= 256:
                break
            if PANEL_REQUESTS[old].get("done") and old != key:
                del PANEL_REQUESTS[old]
    with state["lock"]:
        order = time.time() if order is None else order
        if order <= state["order"]:
            return state, None
        token = uuid.uuid4().hex
        state.update(token=token, order=order, done=False)
    return state, token


def update_panel(respond, user_id, context, client, replace=True, order=None):
    """Send one initial command response; update existing action panels in stages."""
    state, token = begin_panel_request(context, order)
    if token is None:
        return None, state, token
    panel = panel_or_error(user_id, context.get("device_id"), context.get("range", "1h"), client,
                           context.get("custom_start"), context.get("custom_end"),
                           context.get("custom_zone", "America/New_York"), True, context["panel_id"])
    panel["replace_original"] = replace
    with state["lock"]:
        if state["token"] != token:
            return None, state, token
        # Slash-command response URLs do not identify an existing panel to
        # replace. Send only the finished panel on first open. Action response
        # URLs identify their source message and support staged replacement.
        if replace:
            send_panel(respond, panel)
    args = panel.pop("_history_request", None)
    if not args:
        with state["lock"]:
            if state["token"] == token:
                if not replace:
                    send_panel(respond, panel)
                state["done"] = True
        return panel, state, token
    with state["lock"]:
        if state["token"] != token:
            return None, state, token
    try:
        history = history_blocks(*args)
    except Exception as exc:
        print(f"[SLACK HISTORY] {type(exc).__name__}; retry from the panel.", flush=True)
        history = [{"type": "section", "text": plain("Plots unavailable. Click Refresh to retry.")}]
    final = dict(panel)
    final["blocks"] = []
    for block in panel["blocks"]:
        if block.get("block_id") == "hvac_history_pending":
            final["blocks"].extend(history)
        else:
            final["blocks"].append(block)
    final["replace_original"] = replace
    with state["lock"]:
        if state["token"] != token:
            return None, state, token
        send_panel(respond, final)
        state["done"] = True
    return final, state, token


def show_panel(ack, respond, command, client):
    ack()
    update_panel(respond, command.get("user_id"), {}, client, replace=False)


def action_context(body):
    """Merge saved metadata, displayed defaults, live state, then the new action."""
    context = {"device_id": None, "range": "1h", "custom_start": None,
               "custom_end": None, "custom_zone": "America/New_York", "panel_id": None}

    def saved_context(element):
        if element.get("action_id") not in ("hvac_test_refresh", "hvac_custom_edit"):
            return
        try:
            saved = json.loads(element.get("value", "{}"))
            if isinstance(saved, dict):
                context.update({k: saved[k] for k in context if k in saved})
        except (ValueError, TypeError):
            pass

    def selection(action_id, element, field="selected_option"):
        key = {"hvac_sensor_select": "device_id", "hvac_range_select": "range"}.get(action_id)
        option = element.get(field)
        value = option.get("value") if isinstance(option, dict) else None
        # Missing/null selections must not erase the saved companion choice.
        if key and isinstance(value, str) and value:
            context[key] = value

    elements = []
    for block in (body.get("message") or {}).get("blocks", []):
        elements.extend(block.get("elements", []))
        if isinstance(block.get("accessory"), dict):
            elements.append(block["accessory"])
    for element in elements:
        saved_context(element)
    for action in body.get("actions", []):
        saved_context(action)
    for element in elements:
        selection(element.get("action_id"), element, "initial_option")
    # Slack sends the current values of BOTH selectors, not just the changed
    # control. These take precedence over older message/button snapshots.
    for values in (body.get("state") or {}).get("values", {}).values():
        if isinstance(values, dict):
            for action_id, element in values.items():
                if isinstance(element, dict):
                    selection(action_id, element)
    # The exact rendered panel snapshot is authoritative for the companion
    # choice. Apply only the newly changed selector on top of that snapshot.
    remembered = selector_context(body)
    if remembered is not None:
        context.update(remembered)
    for action in body.get("actions", []):
        selection(action.get("action_id"), action)
    if context["range"] not in (*HISTORY_RANGES, "custom"):
        context["range"] = "1h"
    if not isinstance(context["device_id"], str):
        context["device_id"] = None
    return context


def action_device_id(body):
    return action_context(body)["device_id"]


def slack_response_detail(response):
    """Expose Slack's actual rejection reason, without URLs or credentials."""
    body = response if isinstance(response, dict) else getattr(response, "body", None)
    if body is None:
        body = getattr(response, "data", {})
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(body) if isinstance(body, str) else body
        if isinstance(data, dict):
            metadata = data.get("response_metadata") or {}
            detail = json.dumps({"error": data.get("error", "unknown_error"),
                                 "messages": metadata.get("messages", [])}, ensure_ascii=True)
        else:
            detail = str(body)
    except (ValueError, TypeError):
        detail = str(body)
    detail = re.sub(r"https?://[^\s\"<>]+", "[URL REDACTED]", detail)
    detail = re.sub(r"(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+", "[TOKEN REDACTED]", detail)
    return " ".join(detail.split())[:1200] or "No response body"


def send_panel(respond, panel):
    """Use response_url for controls; display private images via bot-token modals.

    The observed response_url endpoint rejects these image blocks with
    HTTP 500 invalid_blocks. Do not keep submitting the same failing payload.
    """
    outgoing = {k: v for k, v in panel.items() if not k.startswith("_")}
    blocks, plots = [], []
    for block in panel.get("blocks", []):
        if block.get("type") != "image":
            blocks.append(block)
        else:
            file_id = block.get("slack_file", {}).get("id")
            if file_id:
                plots.append({"file_id": file_id, "alt": block.get("alt_text", "HVAC history")[:250]})
    if plots:
        plot_button = {"type": "actions", "elements": [{
            "type": "button", "action_id": "hvac_plot_open", "text": plain("View plots"),
            "value": json.dumps({"plots": plots})}]}
        insert_at = next((i for i, block in enumerate(blocks)
                          if block.get("type") == "context" and any(
                              "Alert limits · all sensors" in el.get("text", "")
                              for el in block.get("elements", []))), len(blocks))
        if insert_at and blocks[insert_at-1].get("type") == "divider":
            insert_at -= 1
        blocks.insert(insert_at, plot_button)
    outgoing["blocks"] = blocks
    response = respond(**outgoing)
    status = getattr(response, "status_code", 200)
    if isinstance(status, int) and status != 200:
        print(f"[SLACK PANEL] HTTP {status}: {slack_response_detail(response)}", flush=True)


def finish_plot_modal(client, view_id, image_blocks):
    """Custom-range generation finishes with a fresh-trigger View button.

    Avoid inserting new images into an already-visible loading modal, which has
    repeatedly failed to render on this client despite successful API updates.
    """
    plots = [{"file_id": b["slack_file"]["id"], "alt": b.get("alt_text", "HVAC plot")[:250]}
             for b in image_blocks]
    try:
        client.views_update(view_id=view_id, view={
            "type": "modal", "title": plain("HVAC plots"), "close": plain("Close"),
            "blocks": [{"type": "section", "text": plain("Plots ready.")},
                       {"type": "actions", "elements": [{"type": "button", "action_id": "hvac_plot_open",
                        "text": plain("View plots"), "value": json.dumps({"plots": plots})}]}]})
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("error") if response is not None else None
        if code not in ("not_found", "view_not_found"):
            print("[SLACK PLOT MODAL] Unable to show ready button; use View plots on the panel.", flush=True)


def open_plot(ack, body, client, respond):
    """No rendering/upload here: use the already prepared file and a fresh trigger."""
    ack()
    try:
        action = next(item for item in body.get("actions", [])
                      if item.get("action_id") == "hvac_plot_open")
        value = json.loads(action["value"])
        plots = value.get("plots") or [{"file_id": value.get("file_id"), "alt": value.get("alt")}]
        if not isinstance(plots, list) or not 1 <= len(plots) <= 2:
            raise ValueError("Invalid plot list")
        available_files = tuple(IMAGE_CACHE.values())
        image_blocks = []
        for plot in plots:
            file_id = plot.get("file_id")
            if not isinstance(file_id, str) or not re.fullmatch(r"F[A-Z0-9]+", file_id):
                raise ValueError("Invalid plot file ID")
            if file_id not in available_files:
                respond(response_type="ephemeral", text="This plot snapshot expired. Click Refresh on /hvac, then View Python plots again.")
                return
            image_blocks.append({"type": "image", "slack_file": {"id": file_id},
                                 "alt_text": str(plot.get("alt") or "HVAC history")[:500]})
        # The file checks ran before the button appeared. Open a complete view
        # directly on this fresh click, rather than replacing a loading view.
        links = []
        for i, plot in enumerate(plots):
            link = PLOT_FILE_LINKS.get(plot["file_id"])
            if link:
                links.append({"type": "button", "action_id": f"hvac_file_link_{i}",
                              "text": plain("Open temperature file" if i == 0 else "Open humidity file"),
                              "url": link})
        blocks = [*image_blocks]
        if links:
            blocks.append({"type": "actions", "elements": links})
        view = {"type": "modal", "title": plain("HVAC plots"), "close": plain("Close"), "blocks": blocks}
        if body.get("view"):
            client.views_push(trigger_id=body["trigger_id"], view=view)
        else:
            client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as exc:
        response = getattr(exc, "response", None)
        detail = slack_response_detail(response) if response is not None else type(exc).__name__
        print(f"[SLACK PLOT MODAL] {detail}", flush=True)
        respond(response_type="ephemeral", text=(
            "Slack could not open the plot window. Copy the [SLACK PLOT MODAL] line "
            "from the app terminal. If the panel is old, Refresh and try again."))


def refresh_panel(ack, respond, body, client):
    ack()
    try:
        context = action_context(body)
    except ValueError as exc:
        respond(response_type="ephemeral", text=str(exc))
        return
    actions = body.get("actions", [])
    if any(action.get("action_id") == "hvac_custom_edit" or
           (action.get("action_id") == "hvac_range_select" and
            (action.get("selected_option") or {}).get("value") == "custom") for action in actions):
        open_custom_range(body, client, respond, context)
        return
    try:
        order = float(actions[0]["action_ts"])
        if not math.isfinite(order):
            order = None
    except (KeyError, IndexError, TypeError, ValueError):
        order = None
    update_panel(respond, body.get("user", {}).get("id"), context, client, order=order)


def sensor_options(ack, body):
    # Option requests also travel through Socket Mode; no new callback URL needed.
    try:
        options = load_sensor_options(body.get("value") or "")
    except sqlite3.Error:
        print("Sensor options unavailable; database busy/unavailable.", flush=True)
        options = []
    ack(options=options)


# Retain the ephemeral panel responder only in memory. Slack private_metadata
# contains a random lookup ID, never a response URL, token or other credential.
CUSTOM_REQUESTS = OrderedDict()
CUSTOM_REQUEST_LOCK = threading.Lock()
CUSTOM_ZONES = ("America/New_York", "UTC")


def parse_custom_bounds(start_text, end_text):
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise ValueError("Select a custom start and end time first.")
    start = datetime.fromisoformat(start_text)
    end = datetime.fromisoformat(end_text)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Choose an end time after the start time.")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_time_to_utc(date_text, time_text, zone_name):
    """Reject ambiguous/nonexistent DST wall times rather than guessing an hour."""
    if zone_name not in CUSTOM_ZONES:
        raise ValueError("Select New York time or UTC.")
    naive = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    zone = ZoneInfo(zone_name)
    candidates = set()
    for fold in (0, 1):
        utc = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(utc)
    if not candidates:
        raise ValueError("This time does not exist during the daylight-saving change. Choose another time.")
    if len(candidates) > 1:
        raise ValueError("This time occurs twice during the daylight-saving change. Select UTC instead.")
    return candidates.pop()


def custom_range_modal(context, ticket):
    zone_name = context.get("custom_zone") or "America/New_York"
    if zone_name not in CUSTOM_ZONES:
        zone_name = "America/New_York"
    zone = ZoneInfo(zone_name)
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=1)
    if context.get("custom_start") and context.get("custom_end"):
        start, end = parse_custom_bounds(context["custom_start"], context["custom_end"])
    blocks = [{"type": "section", "text": plain(
        "Choose start and end times (24-hour clock). Start is included; end is excluded.")}]
    zones = [{"text": plain("New York (EST/EDT)"), "value": "America/New_York"},
             {"text": plain("UTC"), "value": "UTC"}]
    blocks.append({"type": "input", "block_id": "plot_timezone", "label": plain("Time zone"),
                   "element": {"type": "static_select", "action_id": "value", "options": zones,
                               "initial_option": next(z for z in zones if z["value"] == zone_name)}})
    for prefix, stamp in (("start", start.astimezone(zone)), ("end", end.astimezone(zone))):
        blocks.append({"type": "input", "block_id": prefix+"_date", "label": plain(prefix.title()+" date"),
                       "element": {"type": "datepicker", "action_id": "value", "initial_date": stamp.strftime("%Y-%m-%d")}})
        blocks.append({"type": "input", "block_id": prefix+"_time", "label": plain(prefix.title()+" time (HH:MM)"),
                       "element": {"type": "timepicker", "action_id": "value", "initial_time": stamp.strftime("%H:%M")}})
    return {"type": "modal", "callback_id": "hvac_custom_range_save", "title": plain("Custom plot range"),
            "submit": plain("View plots"), "close": plain("Cancel"),
            "private_metadata": ticket, "blocks": blocks}


def open_custom_range(body, client, respond, context):
    ticket = uuid.uuid4().hex
    try:
        modal = custom_range_modal(context, ticket)
        with CUSTOM_REQUEST_LOCK:
            now = time.monotonic()
            for key in list(CUSTOM_REQUESTS):
                if now - CUSTOM_REQUESTS[key]["created"] > 900:
                    del CUSTOM_REQUESTS[key]
            CUSTOM_REQUESTS[ticket] = {"created": now, "user_id": body["user"]["id"],
                                       "respond": respond, "context": dict(context)}
            while len(CUSTOM_REQUESTS) > 100:
                CUSTOM_REQUESTS.popitem(last=False)
        client.views_open(trigger_id=body["trigger_id"], view=modal)
    except Exception as exc:
        with CUSTOM_REQUEST_LOCK:
            CUSTOM_REQUESTS.pop(ticket, None)
        message = "Run python -m pip install tzdata, then restart the app." if isinstance(exc, ZoneInfoNotFoundError) else "Unable to open time selection. Try again."
        print(f"[CUSTOM RANGE] {type(exc).__name__}: {message}", flush=True)
        respond(response_type="ephemeral", text=message)


def complete_custom_range(client, view_id, request, context):
    try:
        panel, state, token = update_panel(request["respond"], request["user_id"], context, client)
        with state["lock"]:
            superseded = panel is None or state["token"] != token
        if superseded:
            client.views_update(view_id=view_id, view={"type": "modal", "title": plain("Custom plot range"),
                "close": plain("Close"), "blocks": [{"type": "section", "text": plain(
                    "A newer selection is active. Close and use the current panel.")}]})
            return
        images = [block for block in panel.get("blocks", []) if block.get("type") == "image"]
        if images:
            finish_plot_modal(client, view_id, images)
        else:
            messages = [b["text"]["text"] for b in panel.get("blocks", [])
                        if b.get("type") == "section" and b.get("text", {}).get("text", "").startswith("History:")]
            client.views_update(view_id=view_id, view={"type": "modal", "title": plain("Custom plot range"),
                "close": plain("Close"), "blocks": [{"type": "section", "text": plain(
                    "\n".join(messages) or "Unable to load this range. Close and check the panel.")}]})
    except Exception as exc:
        print(f"[CUSTOM RANGE] Plot request failed ({type(exc).__name__}).", flush=True)
        try:
            client.views_update(view_id=view_id, view={"type": "modal", "title": plain("Custom plot range"),
                "close": plain("Close"), "blocks": [{"type": "section", "text": plain(
                    "Unable to display this range. Close and try again.")}]})
        except Exception:
            pass


def save_custom_range(ack, body, view, client):
    ticket = view.get("private_metadata", "")
    with CUSTOM_REQUEST_LOCK:
        request = CUSTOM_REQUESTS.get(ticket)
    if not request or time.monotonic()-request["created"] > 900 or request["user_id"] != body.get("user", {}).get("id"):
        ack(response_action="errors", errors={"start_date": "Session expired. Close and select Custom dates again."})
        return
    state = view.get("state", {}).get("values", {})
    try:
        zone_name = state["plot_timezone"]["value"]["selected_option"]["value"]
        if zone_name not in CUSTOM_ZONES:
            raise ValueError("Select New York time or UTC.")
    except (KeyError, TypeError, ValueError):
        ack(response_action="errors", errors={"plot_timezone": "Select New York time or UTC."})
        return
    stamps, errors = {}, {}
    for prefix in ("start", "end"):
        try:
            stamps[prefix] = local_time_to_utc(state[prefix+"_date"]["value"]["selected_date"],
                state[prefix+"_time"]["value"]["selected_time"], zone_name)
        except ZoneInfoNotFoundError:
            errors[prefix+"_time"] = "Install tzdata on the receiver PC, then restart the app."
        except (KeyError, TypeError, ValueError) as exc:
            errors[prefix+"_time"] = str(exc)[:180] or "Select a date and time."
    if not errors and stamps["start"] >= stamps["end"]:
        errors["end_time"] = "End time must be after start time."
    if errors:
        ack(response_action="errors", errors=errors)
        return
    context = dict(request["context"])
    context.update({"range": "custom", "custom_start": stamps["start"].isoformat(),
                    "custom_end": stamps["end"].isoformat(), "custom_zone": zone_name})
    # Acknowledge before plotting/uploading. No SDK deadlines depend on plot time.
    ack(response_action="update", view={"type": "modal", "title": plain("Custom plot range"),
        "close": plain("Close"), "blocks": [{"type": "section", "text": plain("Loading plots...")}]})
    with CUSTOM_REQUEST_LOCK:
        CUSTOM_REQUESTS.pop(ticket, None)
    threading.Thread(target=complete_custom_range, args=(client, view["id"], request, context),
                     daemon=True, name="hvac-custom-range").start()


FIELD_LABELS = {
    "temp_min": "Temperature minimum (C)", "temp_max": "Temperature maximum (C)",
    "rh_min": "Humidity minimum (%RH)", "rh_max": "Humidity maximum (%RH)",
}


def settings_modal(settings):
    return {
        "type": "modal", "callback_id": "hvac_settings_save",
        "title": plain("Set alert limits"), "submit": plain("Save"), "close": plain("Cancel"),
        "private_metadata": json.dumps({"version": settings["version"]}),
        "blocks": [
            {"type": "section", "text": plain(
                "These limits apply to ALL sensors. Saving immediately re-evaluates each "
                "sensor's last stored reading, which may be old. Only condition changes "
                "produce an alert/recovery notification. Raw readings are preserved.")},
            *[{"type": "input", "block_id": key, "label": plain(FIELD_LABELS[key]),
               "element": {"type": "plain_text_input", "action_id": "value",
                           "initial_value": f"{settings[key]:g}"}}
              for key in receiver.SETTING_FIELDS],
        ],
    }


def open_settings(ack, body, client, respond):
    ack()
    user_id = body.get("user", {}).get("id")
    if user_id not in receiver.SETTINGS_EDITORS:
        respond(response_type="ephemeral", text="You do not have permission to change alert limits.")
        return
    try:
        settings = receiver.read_settings()
        client.views_open(trigger_id=body["trigger_id"], view=settings_modal(settings))
    except Exception as exc:
        print(f"Unable to open settings ({type(exc).__name__}).", flush=True)
        respond(response_type="ephemeral", text="Unable to open settings. Check the app terminal and retry.")


def save_settings(ack, body, view):
    # Authenticate every submission, even if someone bypasses the hidden button.
    user_id = body.get("user", {}).get("id")
    if user_id not in receiver.SETTINGS_EDITORS:
        ack(response_action="errors", errors={"temp_min": "You do not have permission to edit settings."})
        return
    values, errors = {}, {}
    for key in receiver.SETTING_FIELDS:
        try:
            values[key] = float(view["state"]["values"][key]["value"]["value"])
        except (KeyError, TypeError, ValueError, OverflowError):
            errors[key] = "Enter a number, for example 20 or 20.5."
    if not errors:
        errors = receiver.validate_settings(values)
    if errors:
        ack(response_action="errors", errors=errors)
        return
    try:
        version = json.loads(view["private_metadata"])["version"]
        # The receiver process owns delivery; this process only commits queue entries.
        notify = bool(receiver.slack_url_from_environment())
        result = receiver.update_settings(values, user_id, version, view["id"], notify=notify)
    except (PermissionError, receiver.SettingsConflict, ValueError, KeyError, TypeError) as exc:
        ack(response_action="errors", errors={"temp_min": str(exc)[:200]})
        return
    except sqlite3.Error:
        ack(response_action="errors", errors={"temp_min":
            "Database busy/unavailable. Nothing was saved by this attempt. Retry shortly."})
        return
    print(f"Settings v{result['version']} saved by {user_id}; "
          f"changed={result['changed']}; metric transitions={result['transitions']}", flush=True)
    text = (f"Settings saved (version {result['version']}).\n"
            f"Temperature: {values['temp_min']:g} to {values['temp_max']:g} C\n"
            f"Humidity: {values['rh_min']:g} to {values['rh_max']:g} %RH\n"
            f"Re-evaluation produced {result['transitions']} metric state changes.\n"
            "Refresh /hvac to view the current limits.")
    if result.get("duplicate"):
        text = "This submission was already saved. Refresh /hvac to view the current limits."
    if not notify:
        text += "\nSlack webhook is not configured in this process: no notifications were queued."
    ack(response_action="update", view={"type": "modal", "title": plain("Alert settings"),
        "close": plain("Done"), "blocks": [{"type": "section", "text": plain(text)}]})


def read_token(name, prefix):
    token = os.environ.get(name, "").strip()
    if not token.startswith(prefix) or len(token) <= len(prefix):
        raise ValueError(f"Set {name} in Windows user environment variables "
                         f"(expected prefix {prefix}). Then reopen your terminal.")
    return token


def main():
    try:
        bot_token = read_token("SLACK_BOT_TOKEN", "xoxb-")
        app_token = read_token("SLACK_APP_TOKEN", "xapp-")
    except ValueError as exc:
        print(exc)
        return 1
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        from slack_sdk.errors import SlackApiError
        import matplotlib
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        print("Missing dependency. Run: python -m pip install slack-bolt matplotlib")
        return 1

    # Avoid verbose request logs containing credentials or response URLs.
    logging.basicConfig(level=logging.CRITICAL)
    handler = None
    try:
        if not receiver.DB_PATH.exists():
            print("Start the updated server.py first so data/hvac.db exists.")
            return 1
        stale_seconds()  # Validate presentation settings before connecting.
        gap_seconds()
        receiver.read_settings()  # Validate migration before connecting to Slack.
        print(f"Shared database: {receiver.DB_PATH}", flush=True)
        app = App(token=bot_token)
        app.command("/hvac")(show_panel)
        app.action("hvac_test_refresh")(refresh_panel)
        app.action("hvac_sensor_select")(refresh_panel)
        app.action("hvac_range_select")(refresh_panel)
        app.action("hvac_custom_edit")(refresh_panel)
        app.view("hvac_custom_range_save")(save_custom_range)
        app.options("hvac_sensor_select")(sensor_options)
        app.action("hvac_plot_open")(open_plot)
        app.action(re.compile(r"^hvac_file_link_[0-9]+$"))(lambda ack: ack())
        app.action("hvac_settings_open")(open_settings)
        app.view("hvac_settings_save")(save_settings)

        @app.error
        def on_error(error):
            # Print the exception class, never raw request bodies or secret URLs.
            print(f"Slack interaction failed ({type(error).__name__}). "
                  "Check internet access and the app's permissions.", flush=True)

        handler = SocketModeHandler(app, app_token)
        print("Starting HVAC Slack Python plotting app...", flush=True)
        handler.connect()
        print("Socket Mode connected. In Slack, send /hvac, then select a sensor or click Refresh.",
              flush=True)
        print("Keep this window open. Ctrl+C stops the status app.", flush=True)
        # connect() already opened the socket; do not start another connection.
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nHVAC Slack status app stopped.")
    except SlackApiError as exc:
        # Slack's error code is enough to diagnose authentication/scope errors.
        code = str(exc.response.get("error", "unknown_error"))
        print(f"Slack startup failed: {code}. Check tokens, scopes and installation.")
        return 1
    except Exception as exc:
        print(f"Slack startup failed ({type(exc).__name__}). "
              "Check internet access and Socket Mode settings.")
        return 1
    finally:
        if handler is not None:
            handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
