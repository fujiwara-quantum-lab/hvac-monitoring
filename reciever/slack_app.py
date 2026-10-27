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
    return settings, dict(sensor) if sensor else None, dict(reading) if reading else None, count


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
FILE_UPLOADED_AT = {}  # Monotonic completion times; no persistence or extra scopes.
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
        FILE_UPLOADED_AT[file_id] = time.monotonic()
        while len(IMAGE_CACHE) > 32:
            _, old_id = IMAGE_CACHE.popitem(last=False)
            FILE_UPLOADED_AT.pop(old_id, None)
        return file_id


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
        result = hvac_plot.render_history(history, sensor_name, label, settings, gap_seconds())
        t, rh = result["stats"]["temperature"], result["stats"]["humidity"]
        heading += (f" | {t['n']} raw samples\n"
                    f"Peak-to-peak: temperature {t['ptp']:.3f} C; humidity {rh['ptp']:.3f} %RH")
        if client is None:
            return [{"type": "section", "text": plain(heading + "\nConnect the Slack app to display the image.")}]
        blocks = [{"type": "section", "text": plain(heading)}]
        for plot in result["panels"]:
            file_id = upload_plot(client, plot["png"], f"{sensor_name[:70]} - {plot['title']} - {label}")
            blocks.append({"type": "image", "slack_file": {"id": file_id},
                           "title": plain(plot["title"]),
                           "alt_text": f"{sensor_name[:100]} {plot['title']} raw history."})
        return blocks
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("error") if response is not None else None
        if code == "missing_scope":
            message = "Add files:write under Bot Token Scopes, reinstall the app, and retry."
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


def connection_panel(user_id=None, device_id=None, range_key="1h", client=None,
                     custom_start=None, custom_end=None, custom_zone="America/New_York"):
    if range_key == "custom":
        parse_custom_bounds(custom_start, custom_end)
    if range_key not in (*HISTORY_RANGES, "custom"):
        range_key = "1h"
    settings, sensor, reading, count = sensor_snapshot(device_id)
    now = datetime.now(timezone.utc)
    selected = sensor["device_id"] if sensor else (device_id or "")
    context = {"device_id": selected, "range": range_key, "custom_start": custom_start,
               "custom_end": custom_end, "custom_zone": custom_zone}
    buttons = [{"type": "button", "action_id": "hvac_test_refresh",
                "text": plain("Refresh"), "value": json.dumps(context)}]
    if range_key == "custom":
        buttons.append({"type": "button", "action_id": "hvac_custom_edit",
                        "text": plain("Edit time range"), "value": json.dumps(context)})
    if user_id in receiver.SETTINGS_EDITORS:
        buttons.append({"type": "button", "action_id": "hvac_settings_open",
                        "text": plain("Set alert limits"), "value": "settings"})
    blocks = [{"type": "header", "text": plain("HVAC Monitor - Latest status")}]
    if count:
        select = {"type": "external_select", "action_id": "hvac_sensor_select",
                  "min_query_length": 0, "placeholder": plain("Select or search sensor")}
        if sensor:
            select["initial_option"] = sensor_option(sensor["device_id"], sensor["name"])
        blocks.append({"type": "section", "text": plain(f"Sensor ({count} registered)")})
        blocks.append({"type": "actions", "elements": [select]})
    blocks.append({"type": "section", "text": plain("History window")})
    blocks.append({"type": "actions", "elements": [range_selector(range_key)]})
    if sensor:
        blocks.append({"type": "section", "text": plain(
            f"Sensor: {sensor['name']}\nDevice: {sensor['device_id'].replace(':', '-')}\n" +
            latest_reading_text(reading, settings, now))})
    else:
        blocks.append({"type": "section", "text": plain(
            "The selected sensor no longer exists. Select another sensor." if device_id else
            "No sensors registered yet. Wait for an upload, then click Refresh.")})
    if sensor:
        blocks.extend(history_blocks(selected, range_key, now, settings, client, sensor["name"],
                                     custom_start, custom_end, custom_zone))
    blocks.extend([
        {"type": "section", "text": plain(
            f"Alert limits (all sensors)\n"
            f"Temperature: {settings['temp_min']:g} to {settings['temp_max']:g} C\n"
            f"Humidity: {settings['rh_min']:g} to {settings['rh_max']:g} %RH")},
        {"type": "context", "elements": [plain(
            f"Refreshed (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')} | Manual refresh" +
            ("" if user_id in receiver.SETTINGS_EDITORS else " | View only"))]},
        {"type": "actions", "elements": buttons},
    ])
    return {"response_type": "ephemeral", "text": "HVAC Monitor - Latest stored sensor status",
            "blocks": blocks}


def panel_or_error(user_id, device_id=None, range_key="1h", client=None,
                   custom_start=None, custom_end=None, custom_zone="America/New_York"):
    try:
        return connection_panel(user_id, device_id, range_key, client, custom_start, custom_end, custom_zone)
    except (sqlite3.Error, ValueError):
        # Keep a retry button so a temporary DB lock does not strand the panel.
        return {"response_type": "ephemeral", "text": "HVAC database unavailable.", "blocks": [
            {"type": "section", "text": plain(
                "Database busy/unavailable. Ensure server.py is running in the same folder, then retry.")},
            {"type": "actions", "elements": [{"type": "button", "action_id": "hvac_test_refresh",
             "text": plain("Retry"), "value": json.dumps({"device_id": device_id or "", "range": range_key,
                "custom_start": custom_start, "custom_end": custom_end, "custom_zone": custom_zone})}]}]}


def show_panel(ack, respond, command, client):
    ack()
    send_panel(respond, panel_or_error(command.get("user_id"), client=client))


def action_context(body):
    """Carry the panel's choices in the refresh button, then apply the new action."""
    context = {"device_id": None, "range": "1h", "custom_start": None,
               "custom_end": None, "custom_zone": "America/New_York"}
    for block in body.get("message", {}).get("blocks", []):
        for element in block.get("elements", []):
            if element.get("action_id") == "hvac_test_refresh":
                try:
                    saved = json.loads(element.get("value", "{}"))
                    if isinstance(saved, dict):
                        context.update({k: saved[k] for k in context if k in saved})
                except (ValueError, TypeError):
                    pass
    for action in body.get("actions", []):
        action_id = action.get("action_id")
        if action_id == "hvac_sensor_select":
            context["device_id"] = (action.get("selected_option") or {}).get("value")
        elif action_id == "hvac_range_select":
            context["range"] = (action.get("selected_option") or {}).get("value")
        elif action_id in ("hvac_test_refresh", "hvac_custom_edit"):
            try:
                saved = json.loads(action.get("value", "{}"))
                if isinstance(saved, dict):
                    context.update({k: saved[k] for k in context if k in saved})
            except (ValueError, TypeError):
                pass
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
    outgoing = dict(panel)
    blocks, plots = [], []
    for block in panel.get("blocks", []):
        if block.get("type") != "image":
            blocks.append(block)
        else:
            file_id = block.get("slack_file", {}).get("id")
            if file_id:
                plots.append({"file_id": file_id, "alt": block.get("alt_text", "HVAC history")[:250]})
    if plots:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": "hvac_plot_open", "text": plain("View plots"),
            "value": json.dumps({"plots": plots})}]})
    outgoing["blocks"] = blocks
    response = respond(**outgoing)
    status = getattr(response, "status_code", 200)
    if isinstance(status, int) and status != 200:
        print(f"[SLACK PANEL] HTTP {status}: {slack_response_detail(response)}", flush=True)


def finish_plot_modal(client, view_id, image_blocks):
    """Account for both API rejection and successful-but-not-yet-renderable files.

    Slack does not report whether the user's client rendered an image. For new
    files, allow a processing grace period and then refresh the image blocks twice
    even after API success. This is a bounded workaround, not a readiness signal.
    """
    newest_upload = max((FILE_UPLOADED_AT.get(b.get("slack_file", {}).get("id"), 0)
                         for b in image_blocks), default=0)
    age = time.monotonic()-newest_upload if newest_upload else float("inf")
    fresh_files = age < 60
    if fresh_files and age < 8:
        time.sleep(8-age)  # Loading modal already opened; no trigger_id is held.

    def updated_view():
        # New block IDs prompt Slack to rebuild the image elements on each update.
        blocks = [{**block, "block_id": f"plot-{uuid.uuid4().hex}-{i}"}
                  for i, block in enumerate(image_blocks)]
        return {"type": "modal", "title": plain("HVAC plots"), "close": plain("Close"),
                "blocks": [*blocks, {"type": "context", "elements": [plain("Snapshot from last Refresh.")]}]}

    last_detail = "Unknown error"
    accepted = False
    for attempt, delay in enumerate((0, 1, 2, 4, 6)):
        if delay:
            time.sleep(delay)
        try:
            client.views_update(view_id=view_id, view=updated_view())
            accepted = True
            break
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = response.get("error") if response is not None else None
            if code in ("not_found", "view_not_found"):
                return
            last_detail = slack_response_detail(response) if response is not None else type(exc).__name__
            transient_image = code in ("invalid_arguments", "invalid_blocks") and any(
                phrase in last_detail.lower() for phrase in
                ("invalid slack file", "file_not_found", "file not found", "image_not_ready"))
            if not transient_image:
                break
            if attempt == 0:
                print("[SLACK PLOT MODAL] Waiting for Slack image processing; retrying automatically.", flush=True)
    if accepted:
        if fresh_files:
            # A 200 response does not guarantee the client can load the preview.
            # Reload without asking the user to close and reopen the modal.
            for delay in (4, 8):
                time.sleep(delay)
                try:
                    client.views_update(view_id=view_id, view=updated_view())
                except Exception as exc:
                    response = getattr(exc, "response", None)
                    code = response.get("error") if response is not None else None
                    if code not in ("not_found", "view_not_found"):
                        print("[SLACK PLOT MODAL] Background image refresh unavailable; reopen View plots if needed.", flush=True)
                    return
        return
    print(f"[SLACK PLOT MODAL] Unable to display plot after retries: {last_detail}", flush=True)
    try:
        client.views_update(view_id=view_id, view={
            "type": "modal", "title": plain("HVAC plots"), "close": plain("Close"),
            "blocks": [{"type": "section", "text": plain(
                "Images are not available yet. Close this window and try View plots again shortly.")}]})
    except Exception:
        pass


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
        # Open immediately without image references. The trigger is short-lived;
        # file readiness is handled AFTER opening by updating this specific view.
        opened = client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "title": plain("HVAC plots"), "close": plain("Close"),
            "blocks": [{"type": "section", "text": plain("Loading plots...")}]})
        view_id = opened["view"]["id"]
        threading.Thread(target=finish_plot_modal, args=(client, view_id, image_blocks),
                         daemon=True, name="hvac-plot-modal").start()
    except Exception as exc:
        response = getattr(exc, "response", None)
        detail = slack_response_detail(response) if response is not None else type(exc).__name__
        print(f"[SLACK PLOT MODAL] {detail}", flush=True)
        respond(response_type="ephemeral", text=(
            "Slack could not open the plot window. Copy the [SLACK PLOT MODAL] line "
            "from the app terminal. If the panel is old, Refresh and try again."))


def refresh_panel(ack, respond, body, client):
    ack()
    context = action_context(body)
    actions = body.get("actions", [])
    if any(action.get("action_id") == "hvac_custom_edit" or
           (action.get("action_id") == "hvac_range_select" and
            (action.get("selected_option") or {}).get("value") == "custom") for action in actions):
        open_custom_range(body, client, respond, context)
        return
    panel = panel_or_error(body.get("user", {}).get("id"), context["device_id"], context["range"], client,
                           context["custom_start"], context["custom_end"], context["custom_zone"])
    panel["replace_original"] = True
    send_panel(respond, panel)


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
        panel = panel_or_error(request["user_id"], context["device_id"], "custom", client,
                               context["custom_start"], context["custom_end"], context["custom_zone"])
        panel["replace_original"] = True
        send_panel(request["respond"], panel)
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
