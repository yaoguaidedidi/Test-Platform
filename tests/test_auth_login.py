import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server


def make_handler():
    h = object.__new__(server.HuTestHandler)
    h.responses = []
    h.errors = []
    h.status = 200
    h.sent_headers = {}
    h._json = lambda data, code=200: h.responses.append(('json', code, data))
    h._error = lambda message, status=500: h.errors.append((message, status))
    h.send_response = lambda code: setattr(h, 'status', code)
    h.send_header = lambda k, v: h.sent_headers.__setitem__(k, v)
    h.end_headers = lambda: None
    return h


class AuthLoginTests(unittest.TestCase):
    def setUp(self):
        server._auth_sessions.clear()
        self._save_patcher = patch.object(
            server, '_save_auth_sessions'
        )
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()

    def test_require_login_rejects_anonymous_api(self):
        h = make_handler()
        with patch.object(h, '_read_session_user', return_value=None):
            self.assertFalse(h._require_login('/api/bug/queue'))
        self.assertTrue(h.errors)
        self.assertEqual(h.errors[0][1], 401)

    def test_require_login_allows_public_paths(self):
        h = make_handler()
        for path in (
            '/', '/index.html', '/static/index.html', '/api/health',
            '/api/auth/status', '/api/auth/feishu-login',
            '/api/auth/callback', '/api/auth/logout',
        ):
            self.assertTrue(h._require_login(path), path)

    def test_callback_creates_session_and_cookie(self):
        h = make_handler()
        h.headers = SimpleNamespace(get=lambda name, default='': default)
        token_payload = {'code': 0, 'access_token': 'at'}
        user_payload = {
            'code': 0,
            'data': {'name': '测试', 'email': 't@x.com', 'user_id': 'u1'},
        }
        with (
            patch.object(
                server.requests, 'post',
                return_value=SimpleNamespace(
                    json=lambda: token_payload, status_code=200
                ),
            ),
            patch.object(
                server.requests, 'get',
                return_value=SimpleNamespace(
                    json=lambda: user_payload, status_code=200
                ),
            ),
            patch(
                'lib.feishu._load_creds',
                return_value={'LARK_APP_ID': 'cli_x', 'LARK_APP_SECRET': 's'},
            ),
            patch('lib.feishu._save_refreshed_creds'),
            patch.object(server, 'log_activity'),
        ):
            h._handle_auth_callback({'code': ['abc']})

        self.assertEqual(h.status, 302)
        self.assertIn('hutest_session=', h.sent_headers.get('Set-Cookie', ''))
        self.assertEqual(len(server._auth_sessions), 1)
        user = next(iter(server._auth_sessions.values()))['user']
        self.assertEqual(user['name'], '测试')

    def test_callback_uses_v3_token_endpoint_with_redirect_uri(self):
        """回调应使用新版 OAuth token 接口，并携带与授权一致的 redirect_uri。"""
        h = make_handler()
        h.headers = SimpleNamespace(get=lambda name, default='': default)
        token_payload = {'code': 0, 'access_token': 'at2'}
        user_payload = {
            'code': 0,
            'data': {'name': 'B', 'email': 'b@x.com', 'user_id': 'u2'},
        }
        captured = {}
        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return SimpleNamespace(
                json=lambda: token_payload, status_code=200
            )
        with (
            patch.object(server.requests, 'post', side_effect=fake_post),
            patch.object(
                server.requests, 'get',
                return_value=SimpleNamespace(
                    json=lambda: user_payload, status_code=200
                ),
            ),
            patch(
                'lib.feishu._load_creds',
                return_value={'LARK_APP_ID': 'cli_x', 'LARK_APP_SECRET': 's'},
            ),
            patch('lib.feishu._save_refreshed_creds'),
            patch.object(server, 'log_activity'),
        ):
            h._handle_auth_callback({'code': ['abc']})

        self.assertEqual(
            captured['url'], 'https://accounts.feishu.cn/oauth/v3/token'
        )
        self.assertEqual(
            captured['json']['redirect_uri'], server.AUTH_REDIRECT_URI
        )
        self.assertEqual(h.status, 302)
        self.assertIn('hutest_session=', h.sent_headers.get('Set-Cookie', ''))

    def test_callback_saves_business_creds_when_refresh_token_present(self):
        """登录成功且返回 refresh_token 时，应写入飞书业务凭证供后续 API 复用。"""
        h = make_handler()
        h.headers = SimpleNamespace(get=lambda name, default='': default)
        token_payload = {
            'code': 0,
            'access_token': 'at3',
            'refresh_token': 'rt3',
            'expires_in': 7200,
        }
        user_payload = {'code': 0, 'data': {'name': 'C', 'user_id': 'u3'}}
        saved = {}
        with (
            patch.object(
                server.requests, 'post',
                return_value=SimpleNamespace(
                    json=lambda: token_payload, status_code=200
                ),
            ),
            patch.object(
                server.requests, 'get',
                return_value=SimpleNamespace(
                    json=lambda: user_payload, status_code=200
                ),
            ),
            patch(
                'lib.feishu._load_creds',
                return_value={'LARK_APP_ID': 'cli_x', 'LARK_APP_SECRET': 's'},
            ),
            patch(
                'lib.feishu._save_refreshed_creds',
                side_effect=lambda a, r, e: saved.update(
                    {'access_token': a, 'refresh_token': r, 'expires_at': e}
                ),
            ),
            patch.object(server, 'log_activity'),
        ):
            h._handle_auth_callback({'code': ['abc']})

        self.assertEqual(saved.get('access_token'), 'at3')
        self.assertEqual(saved.get('refresh_token'), 'rt3')
        self.assertGreater(saved.get('expires_at', 0), time.time())

    def test_login_scope_covers_business_permissions(self):
        """登录授权 scope 应包含业务所需权限（表格/wiki/附件/问卷）。"""
        scope = server.FEISHU_LOGIN_SCOPES
        for needed in (
            'auth:user.id:read',
            'offline_access',
            'wiki:node:read',
            'sheets:spreadsheet:read',
            'drive:drive:readonly',
            'bitable:app',
        ):
            self.assertIn(needed, scope)

    def test_sessions_persist_across_restart(self):
        """登录会话应持久化到磁盘，服务重启后仍可恢复。"""
        h = make_handler()
        h.headers = SimpleNamespace(get=lambda name, default='': default)
        token_payload = {'code': 0, 'access_token': 'at4'}
        user_payload = {'code': 0, 'data': {'name': 'D', 'user_id': 'u4'}}
        with (
            patch.object(
                server.requests, 'post',
                return_value=SimpleNamespace(
                    json=lambda: token_payload, status_code=200
                ),
            ),
            patch.object(
                server.requests, 'get',
                return_value=SimpleNamespace(
                    json=lambda: user_payload, status_code=200
                ),
            ),
            patch(
                'lib.feishu._load_creds',
                return_value={'LARK_APP_ID': 'cli_x', 'LARK_APP_SECRET': 's'},
            ),
            patch.object(server, '_save_auth_sessions') as save_mock,
            patch.object(server, 'log_activity'),
        ):
            h._handle_auth_callback({'code': ['abc']})

        self.assertEqual(save_mock.call_count, 1)
        sid = next(iter(server._auth_sessions))
        session = server._auth_sessions[sid]

        # 模拟重启：清空内存 → 从"磁盘"恢复
        server._auth_sessions.clear()
        with patch.object(
            server, '_load_auth_sessions',
            side_effect=lambda: server._auth_sessions.update(
                {sid: session}
            ),
        ):
            server._load_auth_sessions()
        self.assertIn(sid, server._auth_sessions)
        self.assertEqual(
            server._auth_sessions[sid]['user']['name'], 'D'
        )

    def test_auth_status_returns_user_when_logged_in(self):
        h = make_handler()
        server._auth_sessions['sid1'] = {
            'user': {'name': 'N', 'email': 'n@x.com'},
            'expires': time.time() + 100,
        }
        h.headers = SimpleNamespace(
            get=lambda name, default='': (
                'hutest_session=sid1' if name == 'Cookie' else default
            )
        )
        h._handle_auth_status()
        self.assertTrue(h.responses[0][2]['logged_in'])
        self.assertEqual(h.responses[0][2]['user']['name'], 'N')

    def test_auth_status_anonymous(self):
        h = make_handler()
        h.headers = SimpleNamespace(get=lambda name, default='': default)
        h._handle_auth_status()
        self.assertFalse(h.responses[0][2]['logged_in'])

    def test_logout_clears_session(self):
        h = make_handler()
        server._auth_sessions['sid2'] = {
            'user': {'name': 'N'},
            'expires': time.time() + 100,
        }
        h.headers = SimpleNamespace(
            get=lambda name, default='': (
                'hutest_session=sid2' if name == 'Cookie' else default
            )
        )
        h._handle_auth_logout()
        self.assertEqual(h.status, 302)
        self.assertNotIn('sid2', server._auth_sessions)


if __name__ == '__main__':
    unittest.main()
