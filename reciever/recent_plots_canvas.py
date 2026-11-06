"""Dedicated Recent Plots canvas publisher. Uses SLACK_BOT_TOKEN; no Socket Mode.
Run once: python recent_plots_canvas.py --channel C0123456789 --once
Run continuously: python recent_plots_canvas.py
Only this program's dedicated canvas is replaced. Do not add manual notes to it.
"""
import argparse
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
import hvac_plot

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'data' / 'recent_canvas_state.json'
LABELS = {'6h': 'Last 6 hours', '72h': 'Last 72 hours',
          '7d': 'Last 7 days', '30d': 'Last 30 days'}


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    tmp.replace(STATE)


def document(links, cutoff, updated, previous_success=None):
    updated = updated.astimezone(hvac_plot.EASTERN)
    lines = [f'**Last updated: {updated:%Y-%m-%d %H:%M:%S %Z}**',
             'Refresh interval: 5 minutes']
    for key, label in LABELS.items():
        lines.extend([f'## {label}', f'![{label}]({links[key]})'])
    return '\n\n'.join(lines)


def ensure_canvas(client, state):
    if not state.get('canvas_id'):
        result = client.api_call('canvases.create', json={
            'title': 'Recent Plots', 'document_content': {
                'type': 'markdown', 'markdown': 'Waiting for first successful plot update.'}})
        state['canvas_id'] = result['canvas_id']
        save(state)
    info = client.files_info(file=state['canvas_id'])['file']
    link = info.get('permalink')
    if link:
        print('Recent Plots canvas:', link, flush=True)
    if not state.get('shared'):
        client.api_call('canvases.access.set', json={
            'canvas_id': state['canvas_id'], 'channel_ids': [state['channel_id']],
            'access_level': 'read'})
        state['shared'] = True
        save(state)
    return link


def upload(client, path, title):
    # No channel argument: do not post a new image message on each refresh.
    result = client.files_upload_v2(file=str(path), filename=path.name,
                                   title=title, request_file_info=False)
    files = result.get('files') or [result.get('file', {})]
    file_id = files[0].get('id')
    if not file_id:
        raise RuntimeError('Upload returned no file ID')
    for delay in (0, 1, 2, 4, 8, 12):
        if delay:
            time.sleep(delay)
        info = client.files_info(file=file_id)['file']
        if info.get('permalink') and any(k.startswith('thumb_') and v for k,v in info.items()):
            return file_id, info['permalink']
    raise RuntimeError('Image preview not ready; old canvas retained')


def publish(client, state):
    result = hvac_plot.export_recent(ROOT / 'data' / 'hvac.db', ROOT / 'recent_plots')
    links, ids = {}, []
    for key, label in LABELS.items():
        file_id, link = upload(client, Path(result['paths'][key]), 'Recent Plots — '+label)
        ids.append(file_id)
        links[key] = link
    updated = datetime.now(timezone.utc)
    content = document(links, result['data_cutoff_utc'], updated, state.get('last_success_utc'))
    # Single edit only after all four uploads succeed. Failure retains the old
    # dashboard and its old timestamp rather than pretending it refreshed.
    client.api_call('canvases.edit', json={'canvas_id': state['canvas_id'], 'changes': [
        {'operation': 'replace', 'document_content': {'type': 'markdown', 'markdown': content}}]})
    state['last_success_utc'] = updated.isoformat()
    state['current_image_ids'] = ids
    save(state)
    print('Updated all four plots:', state['last_success_utc'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', help='Alert channel ID; required on first run')
    parser.add_argument('--once', action='store_true', help='Publish once and exit')
    args = parser.parse_args()
    # Cross-process guard automatically released on exit, including Windows.
    guard = socket.socket()
    try:
        guard.bind(('127.0.0.1', 48763))
    except OSError:
        parser.exit(1, 'Another publisher is running or port 48763 is occupied.\n')
    token = os.environ.get('SLACK_BOT_TOKEN', '').strip()
    if not token:
        parser.exit(1, 'SLACK_BOT_TOKEN is not set. Use the existing HVAC bot token.\n')
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    channel = args.channel or state.get('channel_id')
    if not channel or not re.fullmatch(r'[CG][A-Z0-9]+', channel):
        parser.exit(1, 'First run: add --channel YOUR_ALERT_CHANNEL_ID\n')
    if state.get('channel_id') and channel != state['channel_id']:
        parser.exit(1, 'Channel differs from saved configuration; no changes made.\n')
    state['channel_id'] = channel
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    client = WebClient(token=token, timeout=30)
    identity = client.auth_test()
    identity_key = identity['team_id'] + ':' + identity['user_id']
    if state.get('identity') and state['identity'] != identity_key:
        parser.exit(1, 'Workspace/bot differs from saved configuration.\n')
    state['identity'] = identity_key
    try:
        ensure_canvas(client, state)
    except SlackApiError as exc:
        parser.exit(1, 'Canvas setup failed: '+str(exc.response.get('error', 'unknown'))+'\n')
    while True:
        started = time.monotonic()
        try:
            publish(client, state)
        except SlackApiError as exc:
            code = str(exc.response.get('error', 'unknown'))
            print('Slack update failed:', code, '(previous plots retained)', flush=True)
            if args.once:
                return 1
        except Exception as exc:
            # Never print raw network exceptions containing upload URLs/tokens.
            print('Update failed:', type(exc).__name__, '(previous plots retained)', flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(1, 300-(time.monotonic()-started)))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Recent Plots publisher stopped.')
