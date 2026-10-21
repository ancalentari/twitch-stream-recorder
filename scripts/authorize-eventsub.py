#!/usr/bin/env python3
"""Authorize EventSub on a headless recorder without exposing access tokens."""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twitch_eventsub import IDENTITY, save_tokens


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=None)
    parser.add_argument('--begin', action='store_true', help='Request a code and exit; continue with --wait')
    parser.add_argument('--wait', action='store_true', help='Wait for an already requested authorization')
    args = parser.parse_args()
    config_path = Path(args.config) if args.config else (
        Path('config/config.json') if Path('config/config.json').exists() else Path('config.json'))
    config = json.loads(config_path.read_text())
    client_id = config['client_id']
    token_path = config.get('eventsub_token_path', 'config/eventsub-token.json')
    device_path = str(Path(token_path).with_name('eventsub-device.json'))
    if args.wait:
        device = json.loads(Path(device_path).read_text())
        if device.get('client_id') != client_id:
            raise RuntimeError('Device authorization belongs to a different app')
    else:
        result = requests.post(IDENTITY + '/device', data={'client_id': client_id, 'scopes': ''}, timeout=15)
        if result.status_code != 200:
            raise RuntimeError('Device authorization request failed (HTTP %s)' % result.status_code)
        device = dict(result.json(), client_id=client_id)
        device['expires_at'] = time.time() + device['expires_in']
        save_tokens(device_path, device)
        print('Open: ' + device['verification_uri'], flush=True)
        print('Code: ' + device['user_code'], flush=True)
        print('Authorize the recorder with your Twitch account. No scopes are requested.', flush=True)
        if args.begin:
            return 0
    interval = max(5, int(device.get('interval', 5)))
    while time.time() < device['expires_at']:
        time.sleep(interval)
        result = requests.post(IDENTITY + '/token', data={
            'client_id': client_id, 'client_secret': config['client_secret'], 'scopes': '',
            'device_code': device['device_code'],
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}, timeout=15)
        if result.status_code == 200:
            data = result.json()
            save_tokens(token_path, dict(data, client_id=client_id,
                                         expires_at=time.time() + data['expires_in']))
            Path(device_path).unlink()
            print('EventSub authorization saved securely. No tokens printed.', flush=True)
            return 0
        error = result.json().get('message', result.json().get('error', 'unknown'))
        if error == 'authorization_pending':
            continue
        if error == 'slow_down':
            interval += 5
            continue
        raise RuntimeError('Device authorization did not complete (HTTP %s)' % result.status_code)
    raise RuntimeError('Authorization code expired; run this command again')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, requests.RequestException, RuntimeError) as exc:
        print('Authorization failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
