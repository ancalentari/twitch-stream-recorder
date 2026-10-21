import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import twitch_eventsub as eventsub
from test_recorder_threading import make_recorder, recorder_module


def response(status, data):
    return SimpleNamespace(status_code=status, json=lambda: data)


def listener(tmp_path, on_online=None):
    return eventsub.EventSubListener('app', 'secret', str(tmp_path / 'tokens.json'),
                                    ['pyka'], on_online or Mock(), threading.Event())


def notification(message_id='event', username='pyka', kind='stream.online'):
    return dict(metadata=dict(message_id=message_id, subscription_type=kind),
                payload=dict(event=dict(broadcaster_user_login=username, id='broadcast')))


def test_duplicates_unconfigured_channels_and_offline_do_not_start_recordings(tmp_path):
    callback = Mock()
    client = listener(tmp_path, callback)
    client.notification(notification())
    client.notification(notification())
    client.notification(notification('unknown', 'someone_else'))
    client.notification(notification('offline', kind='stream.offline'))
    callback.assert_called_once_with('pyka')


def test_duplicate_cache_is_bounded(tmp_path):
    client = listener(tmp_path)
    for index in range(1100):
        client.notification(notification(str(index)))
    assert len(client.seen) == 1000


@pytest.mark.parametrize('url', ['ws://eventsub.wss.twitch.tv/ws', 'wss://evil.example/ws',
                                 'wss://eventsub.wss.twitch.tv.evil.example/ws'])
def test_reconnect_only_uses_twitch_secure_socket(tmp_path, url):
    with pytest.raises(RuntimeError):
        listener(tmp_path).reconnect_url(url)


def test_private_tokens_are_atomically_replaced(tmp_path):
    path = tmp_path / 'tokens.json'
    eventsub.save_tokens(path, {'access_token': 'old'})
    eventsub.save_tokens(path, {'access_token': 'new'})
    assert json.loads(path.read_text())['access_token'] == 'new'
    if os.name == 'posix':
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.eventsub-*'))


def test_refresh_saves_rotated_token_and_validates_identity(monkeypatch, tmp_path):
    path = tmp_path / 'tokens.json'
    eventsub.save_tokens(path, dict(client_id='app', access_token='old', refresh_token='old-refresh', expires_at=0))
    monkeypatch.setattr(eventsub.requests, 'post', Mock(return_value=response(200,
                        dict(access_token='new', refresh_token='new-refresh', expires_in=1000))))
    validate = Mock(return_value=response(200, dict(client_id='app', user_id='me')))
    monkeypatch.setattr(eventsub.requests, 'get', validate)
    tokens = eventsub.UserToken('app', 'secret', str(path))
    assert tokens.get() == 'new'
    assert tokens.get() == 'new'
    assert validate.call_count == 1
    assert json.loads(path.read_text())['refresh_token'] == 'new-refresh'


def test_wrong_app_token_is_rejected_before_api_use(tmp_path):
    path = tmp_path / 'tokens.json'
    eventsub.save_tokens(path, dict(client_id='other', access_token='token', expires_at=time.time()+1000))
    with pytest.raises(RuntimeError, match='different app'):
        eventsub.UserToken('app', 'secret', str(path)).get()


def test_subscriptions_use_user_token_and_both_events(tmp_path):
    client = listener(tmp_path)
    client.broadcasters = {'pyka': '123'}
    client.api = Mock(return_value={})
    client.subscribe('session')
    bodies = [call.kwargs['json'] for call in client.api.call_args_list]
    assert {body['type'] for body in bodies} == {'stream.online', 'stream.offline'}
    assert all(body['transport'] == dict(method='websocket', session_id='session') for body in bodies)


def test_event_callback_uses_existing_recording_check_without_changing_poll_interval(monkeypatch):
    recorder = make_recorder()
    recorder.eventsub_enabled = True
    recorder.eventsub_token_path = 'private.json'
    recorder.client_id, recorder.client_secret = 'app', 'secret'
    recorder.usernames = ['pyka']
    recorder.refresh = 400
    recorder._executor = Mock()
    recorder._reset_user_backoff = Mock()
    captured = []
    def create(*args):
        captured.append(args)
        return SimpleNamespace(start=Mock())
    monkeypatch.setattr(eventsub, 'EventSubListener', create)
    recorder._start_eventsub({'pyka': ('raw', 'processed')})
    captured[0][4]('pyka')
    recorder._executor.submit.assert_called_once_with(recorder._check_eventsub_user, 'pyka', 'raw', 'processed')
    assert recorder.refresh == 400
    recorder._shutdown_event.set()
    captured[0][4]('pyka')
    assert recorder._executor.submit.call_count == 1


def test_socket_loss_resubscribes_and_reconciles(monkeypatch, tmp_path):
    client = listener(tmp_path)
    client.token.get = Mock(return_value='user-token')
    client.broadcasters = {'pyka': '123'}
    client.subscribe = Mock()
    events = []
    client.on_online = events.append
    welcome = json.dumps(dict(metadata=dict(message_type='session_welcome'),
                              payload=dict(session=dict(id='session', keepalive_timeout_seconds=30))))
    class Socket:
        def __init__(self, index):
            self.index = index
            self.messages = [welcome]
        def settimeout(self, seconds):
            pass
        def recv(self):
            if self.messages:
                return self.messages.pop()
            if self.index == 1:
                client.shutdown.set()
            return ''
        def close(self):
            pass
    count = []
    def connect(*args, **kwargs):
        socket = Socket(len(count))
        count.append(socket)
        return socket
    monkeypatch.setitem(sys.modules, 'websocket', SimpleNamespace(create_connection=connect,
                        WebSocketTimeoutException=TimeoutError))
    monkeypatch.setattr(client.shutdown, 'wait', lambda timeout: client.shutdown.is_set())
    client.run()
    assert len(count) == 2
    assert client.subscribe.call_count == 2
    assert events == ['pyka', 'pyka']


def test_server_migration_preserves_subscriptions(monkeypatch, tmp_path):
    client = listener(tmp_path)
    client.token.get = Mock(return_value='user-token')
    client.broadcasters = {'pyka': '123'}
    client.subscribe = Mock()
    def welcome(session):
        return json.dumps(dict(metadata=dict(message_type='session_welcome'),
                               payload=dict(session=dict(id=session, keepalive_timeout_seconds=30))))
    reconnect = json.dumps(dict(metadata=dict(message_type='session_reconnect'), payload=dict(
        session=dict(reconnect_url='wss://eventsub.wss.twitch.tv/ws?reconnect=example'))))
    calls = []
    class Socket:
        def __init__(self, messages):
            self.messages = messages
            self.closed = False
        def settimeout(self, timeout):
            pass
        def recv(self):
            if self.messages:
                return self.messages.pop(0)
            client.shutdown.set()
            return ''
        def close(self):
            self.closed = True
    old = Socket([welcome('old'), reconnect])
    replacement = Socket([welcome('new')])
    def connect(url, **kwargs):
        calls.append(url)
        if len(calls) == 2:
            assert not old.closed
            return replacement
        return old
    monkeypatch.setitem(sys.modules, 'websocket', SimpleNamespace(create_connection=connect,
                        WebSocketTimeoutException=TimeoutError))
    client.run()
    client.subscribe.assert_called_once_with('old')
    assert old.closed and replacement.closed
    assert len(calls) == 2


def test_late_keepalive_within_grace_does_not_reconnect(monkeypatch, tmp_path):
    client = listener(tmp_path)
    client.token.get = Mock(return_value='user-token')
    client.broadcasters = {'pyka': '123'}
    client.subscribe = Mock()
    clock = [0]
    monkeypatch.setattr(eventsub.time, 'monotonic', lambda: clock[0])
    welcome = json.dumps(dict(metadata=dict(message_type='session_welcome'),
                              payload=dict(session=dict(id='session', keepalive_timeout_seconds=30))))
    class Socket:
        calls = 0
        def settimeout(self, seconds):
            assert seconds == 1  # Shutdown remains responsive during quiet periods.
        def recv(self):
            self.calls += 1
            if self.calls == 1:
                return welcome
            if self.calls == 2:
                clock[0] = 30.16
                raise TimeoutError()
            clock[0] = 32
            client.shutdown.set()
            return json.dumps(dict(metadata=dict(message_type='session_keepalive'), payload={}))
        def close(self):
            pass
    connect = Mock(return_value=Socket())
    monkeypatch.setitem(sys.modules, 'websocket', SimpleNamespace(create_connection=connect,
                        WebSocketTimeoutException=TimeoutError))
    client.run()
    assert connect.call_count == 1
    assert client.failure_count == 0
    client.subscribe.assert_called_once_with('session')


def test_silent_connection_expires_after_negotiated_timeout_and_grace(monkeypatch, tmp_path):
    client = listener(tmp_path)
    client.token.get = Mock(return_value='user-token')
    client.broadcasters = {'pyka': '123'}
    client.subscribe = Mock()
    clock = [0]
    monkeypatch.setattr(eventsub.time, 'monotonic', lambda: clock[0])
    welcome = json.dumps(dict(metadata=dict(message_type='session_welcome'),
                              payload=dict(session=dict(id='session', keepalive_timeout_seconds=10))))
    class Socket:
        calls = 0
        def settimeout(self, seconds):
            pass
        def recv(self):
            self.calls += 1
            if self.calls == 1:
                return welcome
            clock[0] = 16
            raise TimeoutError()
        def close(self):
            pass
    def wait(timeout):
        assert not client.connected.is_set()  # Enables the existing outage polling.
        client.shutdown.set()
        return True
    monkeypatch.setattr(client.shutdown, 'wait', wait)
    connect = Mock(return_value=Socket())
    monkeypatch.setitem(sys.modules, 'websocket', SimpleNamespace(create_connection=connect,
                        WebSocketTimeoutException=TimeoutError))
    client.run()
    assert connect.call_count == 1
    assert client.failure_count == 1
    assert 'no message for 16.0s' in client.last_error
    assert 'Twitch timeout 10s + 5s grace' in client.last_error


def test_device_login_requests_no_scopes_and_saves_private_tokens(monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('authorize_eventsub',
        Path(__file__).resolve().parents[1] / 'scripts' / 'authorize-eventsub.py')
    authorize = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(authorize)
    config = tmp_path / 'config.json'
    token_path = tmp_path / 'tokens.json'
    config.write_text(json.dumps(dict(client_id='app', client_secret='secret', eventsub_token_path=str(token_path))))
    responses = [response(200, dict(device_code='private-device', user_code='PUBLIC',
                   verification_uri='https://www.twitch.tv/activate', expires_in=1000, interval=5)),
                 response(400, dict(message='authorization_pending')),
                 response(200, dict(access_token='private-access', refresh_token='private-refresh', expires_in=1000))]
    post = Mock(side_effect=responses)
    monkeypatch.setattr(authorize.requests, 'post', post)
    monkeypatch.setattr(authorize.time, 'sleep', lambda timeout: None)
    monkeypatch.setattr(sys, 'argv', ['authorize-eventsub.py', '--config', str(config)])
    assert authorize.main() == 0
    output = capsys.readouterr().out
    assert 'PUBLIC' in output
    assert 'private-device' not in output and 'private-access' not in output and 'private-refresh' not in output
    assert all(call.kwargs['data']['scopes'] == '' for call in post.call_args_list)
    assert json.loads(token_path.read_text())['client_id'] == 'app'
    assert not (tmp_path / 'eventsub-device.json').exists()


@pytest.mark.parametrize('connected,expected', [(True, False), (False, True)])
def test_routine_polling_only_runs_during_eventsub_outage(connected, expected):
    recorder = make_recorder()
    healthy = threading.Event()
    if connected:
        healthy.set()
    recorder._eventsub_listener = SimpleNamespace(connected=healthy)
    assert recorder._should_poll_user('pyka') is expected


def test_disabled_eventsub_keeps_polling():
    assert make_recorder()._should_poll_user('pyka')


def test_recording_recovery_is_checked_until_recording_starts(monkeypatch):
    recorder = make_recorder()
    healthy = threading.Event()
    healthy.set()
    recorder._eventsub_listener = SimpleNamespace(connected=healthy)
    recorder._eventsub_recovery_users.add('pyka')
    assert recorder._should_poll_user('pyka')
    monkeypatch.setattr(recorder, '_is_user_recording', lambda username: True)
    assert not recorder._should_poll_user('pyka')
    assert 'pyka' not in recorder._eventsub_recovery_users


def test_confirmed_offline_stops_recovery_even_with_backoff_disabled():
    recorder = make_recorder()
    recorder.offline_backoff_enabled = False
    recorder._eventsub_recovery_users.add('pyka')
    recorder._update_user_check_schedule('pyka', recorder_module.TwitchResponseStatus.OFFLINE)
    assert 'pyka' not in recorder._eventsub_recovery_users


def test_startup_offline_check_does_not_leave_periodic_recovery(monkeypatch):
    recorder = make_recorder()
    recorder.offline_backoff_enabled = False
    recorder._eventsub_recovery_users.add('pyka')
    monkeypatch.setattr(recorder, 'check_and_record_user', lambda *args: recorder_module.TwitchResponseStatus.OFFLINE)
    recorder._check_eventsub_user('pyka', 'raw', 'processed')
    assert not recorder._eventsub_recovery_users
