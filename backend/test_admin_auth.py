import asyncio
import importlib
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend.admin_auth import AdminAuth, SESSION_SECONDS

PASSWORD = 'test-admin-password-for-tests-only'


class AdminAuthTests(unittest.TestCase):
    def test_empty_or_short_password_fails_closed(self):
        for password in ['', 'short']:
            auth = AdminAuth(password)
            with self.assertRaises(HTTPException) as error:
                auth.login(password, 'client')
            self.assertEqual(error.exception.status_code, 503)
            with self.assertRaises(HTTPException):
                auth.require(None)

    def test_token_is_random_expires_and_logout_revokes_it(self):
        now = [100]
        auth = AdminAuth(PASSWORD, clock=lambda: now[0])
        first = auth.login(PASSWORD, 'client')['access_token']
        second = auth.login(PASSWORD, 'client')['access_token']
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, PASSWORD)
        self.assertNotIn(first, auth.sessions)
        self.assertEqual(auth.require(first), first)
        auth.logout(first)
        with self.assertRaises(HTTPException):
            auth.require(first)
        now[0] += SESSION_SECONDS
        with self.assertRaises(HTTPException):
            auth.require(second)

    def test_login_attempts_are_limited_and_recover_after_window(self):
        now = [0]
        auth = AdminAuth(PASSWORD, clock=lambda: now[0])
        for _ in range(5):
            with self.assertRaises(HTTPException) as failure:
                auth.login('incorrect', 'client')
            self.assertEqual(failure.exception.status_code, 401)
        with self.assertRaises(HTTPException) as limited:
            auth.login(PASSWORD, 'client')
        self.assertEqual(limited.exception.status_code, 429)
        self.assertEqual(limited.exception.headers['Retry-After'], '60')
        self.assertIn('access_token', auth.login(PASSWORD, 'different-client'))
        now[0] = 61
        self.assertIn('access_token', auth.login(PASSWORD, 'client'))


async def request(app, method, path, payload=None, token=None):
    body = json.dumps(payload).encode() if payload is not None else b''
    headers = [(b'host', b'localhost'), (b'content-type', b'application/json')]
    if token:
        headers.append((b'authorization', f'Bearer {token}'.encode()))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'path': path, 'raw_path': path.encode(), 'root_path': '',
             'query_string': b'', 'headers': headers, 'scheme': 'http',
             'server': ('localhost', 8000), 'client': ('127.0.0.1', 1234)}
    messages = []
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(item for item in messages if item['type'] == 'http.response.start')
    data = b''.join(item.get('body', b'') for item in messages if item['type'] == 'http.response.body')
    return start['status'], json.loads(data) if data else {}, dict(start['headers'])


class AdminRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = importlib.import_module('backend.app')
        self.auth = AdminAuth(PASSWORD)
        self.auth_patch = patch('backend.admin_auth.admin_auth', self.auth)
        self.api_patch = patch.object(self.api, 'admin_auth', self.auth)
        self.auth_patch.start()
        self.api_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.api_patch.stop)

    async def test_all_settings_and_reset_routes_require_login(self):
        for method, path, data in [('GET', '/admin', None), ('PUT', '/admin', {}),
                                   ('POST', '/admin/reset-dashboard', None)]:
            status, _, _ = await request(self.api.app, method, path, data)
            self.assertEqual(status, 401, path)
        status, _, _ = await request(self.api.app, 'GET', '/admin', token='forged-token')
        self.assertEqual(status, 401)

    async def test_login_allows_admin_requests_and_logout_removes_access(self):
        status, session, headers = await request(self.api.app, 'POST', '/admin/login', {'password': PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(headers[b'cache-control'], b'no-store')
        token = session['access_token']
        status, settings, _ = await request(self.api.app, 'GET', '/admin', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(settings['settings']['model_key'], 'tensorflow_50')
        with patch.object(self.api.game_manager, 'reset_dashboard', return_value={'ok': True}) as reset:
            status, _, _ = await request(self.api.app, 'POST', '/admin/reset-dashboard', token=token)
            self.assertEqual(status, 200)
            reset.assert_called_once()
        with patch.object(self.api.game_manager, 'update_admin_settings', return_value={}) as save:
            status, _, _ = await request(self.api.app, 'PUT', '/admin', {'model_key': 'tensorflow_50', 'round_seconds': 40}, token=token)
            self.assertEqual(status, 200)
            save.assert_called_once()
        self.assertEqual((await request(self.api.app, 'POST', '/admin/logout', token=token))[0], 200)
        self.assertEqual((await request(self.api.app, 'GET', '/admin', token=token))[0], 401)

    async def test_public_health_and_game_entry_are_not_password_gated(self):
        self.assertEqual((await request(self.api.app, 'GET', '/health'))[0], 200)
        with patch.object(self.api.game_manager, 'leaderboard', return_value={'players': [], 'games': []}):
            self.assertEqual((await request(self.api.app, 'GET', '/leaderboard'))[0], 200)

    async def test_wrong_password_does_not_grant_access(self):
        status, response, _ = await request(self.api.app, 'POST', '/admin/login', {'password': 'incorrect'})
        self.assertEqual(status, 401)
        self.assertNotIn('access_token', response)
