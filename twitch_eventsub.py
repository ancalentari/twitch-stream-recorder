"""Twitch EventSub notifications; failure always leaves recorder polling available."""
import collections
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = 'https://api.twitch.tv/helix'
IDENTITY = 'https://id.twitch.tv/oauth2'
SOCKET_URL = 'wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30'
# Arrival can lag Twitch's send deadline due to network and Pi scheduling jitter.
# Keep this bounded so a genuinely silent connection still switches to polling.
KEEPALIVE_GRACE_SECONDS = 5


def save_tokens(path, tokens):
    """Publish private OAuth credentials atomically, including rotated refresh tokens."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.eventsub-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(tokens, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class UserToken:
    def __init__(self, client_id, client_secret, path):
        self.client_id = client_id
        self.client_secret = client_secret
        self.path = path
        self.tokens = None
        self.validated_at = 0

    def get(self):
        if self.tokens is None:
            self.tokens = json.loads(Path(self.path).read_text())
            if self.tokens.get('client_id') != self.client_id:
                raise RuntimeError('EventSub authorization belongs to a different app')
        if time.time() >= self.tokens.get('expires_at', 0) - 300:
            self.refresh()
        if not self.validated_at or time.monotonic() - self.validated_at >= 3600:
            response = requests.get(IDENTITY + '/validate', headers={
                'Authorization': 'OAuth ' + self.tokens['access_token']}, timeout=15)
            if response.status_code == 401:
                self.refresh()
                response = requests.get(IDENTITY + '/validate', headers={
                    'Authorization': 'OAuth ' + self.tokens['access_token']}, timeout=15)
            if response.status_code != 200:
                raise RuntimeError('EventSub token validation failed (HTTP %s)' % response.status_code)
            data = response.json()
            if data.get('client_id') != self.client_id or not data.get('user_id'):
                raise RuntimeError('EventSub requires a user token for this app')
            self.validated_at = time.monotonic()
        return self.tokens['access_token']

    def refresh(self):
        response = requests.post(IDENTITY + '/token', data={
            'grant_type': 'refresh_token', 'refresh_token': self.tokens['refresh_token'],
            'client_id': self.client_id, 'client_secret': self.client_secret}, timeout=15)
        if response.status_code != 200:
            self.tokens = None
            raise RuntimeError('EventSub authorization needs renewal (HTTP %s)' % response.status_code)
        data = response.json()
        tokens = dict(data, client_id=self.client_id,
                      expires_at=time.time() + data['expires_in'])
        tokens.setdefault('refresh_token', self.tokens['refresh_token'])
        save_tokens(self.path, tokens)
        self.tokens = tokens
        self.validated_at = 0


class EventSubListener(threading.Thread):
    def __init__(self, client_id, client_secret, token_path, usernames, on_online, shutdown):
        super().__init__(name='twitch-eventsub', daemon=True)
        self.token = UserToken(client_id, client_secret, token_path)
        self.client_id = client_id
        self.usernames = {name.lower() for name in usernames}
        self.on_online = on_online
        self.shutdown = shutdown
        self.seen = collections.OrderedDict()
        self.connected = threading.Event()
        self.broadcasters = None
        self.last_message_at = None
        self.last_event_at = None
        self.connected_since = None
        self.failure_count = 0
        self.last_error = None
        self.keepalive_seconds = 30

    def health_snapshot(self):
        return dict(connected=self.connected.is_set(), last_message_at=self.last_message_at,
                    last_event_at=self.last_event_at, connected_since=self.connected_since,
                    failure_count=self.failure_count, last_error=self.last_error,
                    listener_alive=self.is_alive(), keepalive_seconds=self.keepalive_seconds)

    def api(self, method, path, **kwargs):
        response = requests.request(method, API + path, headers={
            'Client-Id': self.client_id, 'Authorization': 'Bearer ' + self.token.get()
        }, timeout=15, **kwargs)
        if response.status_code == 401:
            self.token.validated_at = 0
            response = requests.request(method, API + path, headers={
                'Client-Id': self.client_id, 'Authorization': 'Bearer ' + self.token.get()
            }, timeout=15, **kwargs)
        if response.status_code not in (200, 202):
            raise RuntimeError('EventSub API %s failed (HTTP %s)' % (path, response.status_code))
        return response.json()

    def subscribe(self, session_id):
        if self.broadcasters is None:
            data = self.api('GET', '/users', params=[('login', name) for name in sorted(self.usernames)])
            self.broadcasters = {user['login'].lower(): user['id'] for user in data['data']}
            if set(self.broadcasters) != self.usernames:
                raise RuntimeError('Could not resolve every configured Twitch username')
        for username, broadcaster in self.broadcasters.items():
            for event in ('stream.online', 'stream.offline'):
                self.api('POST', '/eventsub/subscriptions', json={
                    'type': event, 'version': '1',
                    'condition': {'broadcaster_user_id': broadcaster},
                    'transport': {'method': 'websocket', 'session_id': session_id}})
        logging.info('EventSub subscribed to online/offline events for %s', ', '.join(sorted(self.usernames)))

    def notification(self, message):
        metadata = message['metadata']
        message_id = metadata['message_id']
        if message_id in self.seen:
            return
        self.seen[message_id] = None
        if len(self.seen) > 1000:
            self.seen.popitem(last=False)
        event = message['payload']['event']
        username = event.get('broadcaster_user_login', '').lower()
        if username not in self.usernames:
            return
        kind = metadata.get('subscription_type')
        if kind == 'stream.online':
            logging.info('EventSub: %s went online (broadcast %s)', username, event.get('id'))
            self.on_online(username)
        elif kind == 'stream.offline':
            # Let Streamlink finish draining buffered footage before stopping.
            logging.info('EventSub: %s went offline', username)

    @staticmethod
    def reconnect_url(url):
        parsed = urlparse(url)
        if parsed.scheme != 'wss' or parsed.hostname != 'eventsub.wss.twitch.tv' or parsed.port not in (None, 443):
            raise RuntimeError('Unexpected EventSub reconnect URL')
        return url

    def run(self):
        try:
            import websocket
        except ImportError:
            logging.warning('EventSub unavailable: install websocket-client; polling remains active')
            return
        retry_delay = 5
        while not self.shutdown.is_set():
            socket = None
            try:
                self.token.get()
                # Resolve IDs before opening the socket to respect the welcome deadline.
                if self.broadcasters is None:
                    data = self.api('GET', '/users', params=[('login', name) for name in sorted(self.usernames)])
                    self.broadcasters = {u['login'].lower(): u['id'] for u in data['data']}
                    if set(self.broadcasters) != self.usernames:
                        raise RuntimeError('Could not resolve every configured Twitch username')
                socket = websocket.create_connection(SOCKET_URL, timeout=15)
                socket.settimeout(1)
                keepalive = 30
                received_at = time.monotonic()
                deadline = received_at + keepalive + KEEPALIVE_GRACE_SECONDS
                while not self.shutdown.is_set():
                    try:
                        raw = socket.recv()
                    except websocket.WebSocketTimeoutException:
                        self.token.get()  # Refresh and validate even during quiet periods.
                        if time.monotonic() >= deadline:
                            raise RuntimeError('EventSub keepalive expired: no message for %.1fs '
                                               '(Twitch timeout %ss + %ss grace)' % (
                                                   time.monotonic() - received_at, keepalive,
                                                   KEEPALIVE_GRACE_SECONDS))
                        continue
                    if not raw:
                        raise RuntimeError('EventSub connection closed')
                    message = json.loads(raw)
                    received_at = time.monotonic()
                    kind = message['metadata']['message_type']
                    self.last_message_at = time.time()
                    if kind == 'session_welcome':
                        session = message['payload']['session']
                        keepalive = session.get('keepalive_timeout_seconds') or 30
                        self.keepalive_seconds = keepalive
                        self.subscribe(session['id'])
                        # Recover broadcasts already live or missed while disconnected.
                        for username in sorted(self.usernames):
                            self.on_online(username)
                        self.connected.set()
                        self.connected_since = time.time()
                        self.last_error = None
                        retry_delay = 5
                        logging.info('EventSub connected; routine polling suspended; outage fallback enabled')
                    elif kind == 'notification':
                        self.last_event_at = time.time()
                        self.notification(message)
                    elif kind == 'session_reconnect':
                        url = self.reconnect_url(message['payload']['session']['reconnect_url'])
                        replacement = websocket.create_connection(url, timeout=15)
                        # Keep the old connection until the new welcome confirms migration.
                        try:
                            welcome = json.loads(replacement.recv())
                            if welcome['metadata']['message_type'] != 'session_welcome':
                                raise RuntimeError('EventSub reconnect did not receive welcome')
                        except Exception:
                            replacement.close()
                            raise
                        socket.close()
                        socket = replacement
                        socket.settimeout(1)
                        keepalive = welcome['payload']['session'].get('keepalive_timeout_seconds') or 30
                        self.keepalive_seconds = keepalive
                        self.last_message_at = time.time()
                        received_at = time.monotonic()
                        logging.info('EventSub connection migrated')
                        for username in sorted(self.usernames):
                            self.on_online(username)
                    elif kind == 'revocation':
                        self.broadcasters = None
                        raise RuntimeError('EventSub subscription revoked; polling remains active')
                    deadline = received_at + keepalive + KEEPALIVE_GRACE_SECONDS
            except Exception as exc:
                self.connected.clear()
                self.failure_count += 1
                self.last_error = type(exc).__name__ + ': ' + str(exc)
                # Never include OAuth responses or secrets in logs.
                logging.warning('EventSub unavailable (%s); polling remains active; retry in %ss',
                                type(exc).__name__ + ': ' + str(exc), retry_delay)
            finally:
                self.connected.clear()
                if socket:
                    try:
                        socket.close()
                    except Exception:
                        pass
            if self.shutdown.wait(retry_delay):
                break
            retry_delay = min(300, retry_delay * 2)
