import unittest
from unittest.mock import patch
import os
import tempfile
import time

import server


class FakeCapture:
    def __init__(self, existing_id=''):
        self.problem_data = {
            'id': 'abc123', 'session_id': 'session-1', 'kind': 'crash',
            'severity': 'critical', 'title': 'FATAL EXCEPTION: main',
            'message': 'FATAL EXCEPTION: main', 'source': '123/AndroidRuntime',
            'first_at': '07-22 12:00:00.000',
            'last_at': '07-22 12:00:01.000', 'count': 2,
            'last_epoch': 1234.5,
            'bug_id': existing_id,
            'context': [{'time': '12:00', 'source': 'Unity', 'message': 'boom'}],
        }
        self.marked = None

    def problem(self, problem_id, session_id=''):
        return dict(self.problem_data)

    def problem_snapshot(self, problem_id, output_dir, session_id=''):
        return '/tmp/full-log.zip', dict(self.problem_data)

    def session_snapshot(self, output_dir, session_id=''):
        return '/tmp/manual-log.zip', {
            'id': 'session-1', 'app_name': 'Dream',
            'device_id': 'emulator-5554', 'started_at': '2026-07-27T10:00:00',
            'line_count': 88,
        }

    def status(self):
        return {'session': {
            'app_name': 'Dream', 'device_id': 'emulator-5554',
            'started_at': '2026-07-22T10:00:00',
        }}

    def mark_problem_bug(self, problem_id, bug_id, session_id=''):
        self.marked = (problem_id, bug_id, session_id)


class FakeRecorder:
    def __init__(self, active=True):
        self.active = active
        self.snapshots = []
        self.anchors = []

    def status(self):
        return {'session': {
            'id': 'session-1', 'active': self.active,
            'available_seconds': 60,
        }}

    def has_session(self, session_id):
        return session_id == 'session-1'

    def can_recover_window(self, session_id, anchor_time,
                           before_seconds=None, after_seconds=None):
        return True

    def snapshot(self, output_dir, session_id='', anchor_time=None,
                 before_seconds=None, after_seconds=None):
        self.snapshots.append((
            output_dir, session_id, before_seconds, after_seconds,
        ))
        self.anchors.append((session_id, anchor_time))
        fake_path = '/tmp/recent-minute.mp4'
        if not os.path.exists(fake_path):
            with open(fake_path, 'w') as f:
                f.write('fake video bytes')
        return fake_path, {
            'duration': 60, 'before_seconds': 40, 'after_seconds': 20,
            'size': 1234,
        }


class LogProblemBugTests(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)
        return handler, response

    def test_latest_error_time_finds_last_error_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, 'sample.log')
            with open(log_path, 'w', encoding='utf-8') as handle:
                handle.write(
                    '08-07 17:31:42.967 [INFO] 100/Unity hello\n'
                    '08-07 17:33:48.642 [ERROR] 14360/Unity boom\n'
                    '08-07 17:33:48.919 [ERROR] 14360/Unity boom again\n'
                    '08-07 17:34:00.100 [WARNING] 14360/Unity later\n'
                )
            self.assertEqual(
                server._latest_error_time(log_path),
                '08-07 17:33:48.919',
            )

    def test_latest_error_time_returns_none_without_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, 'quiet.log')
            with open(log_path, 'w', encoding='utf-8') as handle:
                handle.write('08-07 17:31:42.967 [INFO] 100/Unity hello\n')
            self.assertIsNone(server._latest_error_time(log_path))

    def test_log_time_to_epoch_uses_session_year(self):
        epoch = server._log_time_to_epoch(
            '07-22 12:00:01.000',
            {'started_at': '2026-07-22T10:00:00'},
        )
        self.assertIsNotNone(epoch)
        self.assertEqual(
            server._log_time_to_epoch('', {'started_at': 'x'}), None
        )

    def test_creates_scripted_preview_with_full_log_without_ai(self):
        handler, response = self.make_handler()
        capture = FakeCapture()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, 'save_draft', return_value=('bug-1', '/tmp/x')) as save,
            patch.object(server, 'log_activity'),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_log_problem_bug('abc123', {'assignee': '萌虎'})

        self.assertEqual(response['id'], 'bug-1')
        self.assertEqual(save.call_args.kwargs['mode'], 'scripted')
        self.assertEqual(save.call_args.kwargs['attachments'], ['/tmp/full-log.zip'])
        preview = save.call_args.kwargs['preview']
        self.assertIn('FATAL EXCEPTION', preview['title'])
        self.assertIn('完整带类型日志见附件', preview['description'])
        self.assertNotIn('最近一次上下文', preview['description'])
        self.assertNotIn('boom', preview['description'])
        self.assertEqual(preview['severity'], '1')
        self.assertEqual(capture.marked, ('abc123', 'bug-1', ''))
        popen.assert_not_called()

    def test_reuses_existing_bug_for_same_problem(self):
        handler, response = self.make_handler()
        capture = FakeCapture(existing_id='bug-old')

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, 'get_bug', return_value={
                'id': 'bug-old', 'status': 'preview_ready',
            }),
            patch.object(server, 'save_draft') as save,
        ):
            handler._handle_log_problem_bug('abc123', {})

        self.assertEqual(response['id'], 'bug-old')
        self.assertTrue(response['existing'])
        save.assert_not_called()

    def test_manual_log_bug_uses_editable_scripted_preview(self):
        handler, response = self.make_handler()
        capture = FakeCapture()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, 'save_draft', return_value=('manual-1', '/tmp/x')) as save,
            patch.object(server, 'log_activity'),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_log_session_bug({'assignee': '萌虎'})

        self.assertEqual(response['id'], 'manual-1')
        self.assertEqual(save.call_args.kwargs['mode'], 'scripted')
        self.assertEqual(
            save.call_args.kwargs['attachments'], ['/tmp/manual-log.zip']
        )
        preview = save.call_args.kwargs['preview']
        self.assertIn('请补充问题标题', preview['title'])
        self.assertIn('请在这里补充问题表现', preview['description'])
        popen.assert_not_called()

    def test_manual_log_bug_attaches_recent_recording_when_enabled(self):
        handler, response = self.make_handler()
        capture = FakeCapture()
        recorder = FakeRecorder()
        fake_bug = {
            'preview': {
                'description': (
                    '【日志摘要】\n完整带类型日志见附件，可下载查看。\n'
                    '录屏生成中，稍后自动附加'
                    '（点击提交前 40 秒 + 点击提交后 20 秒）。'
                ),
            },
            'bug': {'attachments': ['/tmp/manual-log.zip']},
        }

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(
                server, 'save_draft', return_value=('manual-video', '/tmp/x')
            ) as save,
            patch.object(server, 'get_bug', return_value=fake_bug),
            patch.object(server, 'update_bug') as update,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_session_bug({'assignee': '萌虎'})
            time.sleep(0.6)

        # 同步立即返回：界面先出，附件仅日志，录屏标记为生成中
        self.assertEqual(response['recording_status'], 'pending')
        self.assertEqual(
            save.call_args.kwargs['attachments'], ['/tmp/manual-log.zip']
        )
        self.assertEqual(save.call_args.kwargs['recording_status'], 'pending')
        self.assertIn(
            '录屏生成中', save.call_args.kwargs['preview']['description']
        )
        # 后台线程以点击提交时刻为锚点生成录屏
        self.assertEqual(recorder.snapshots[0][1], 'session-1')
        anchor = recorder.anchors[0][1]
        self.assertIsNotNone(anchor)
        self.assertLess(abs(anchor - time.time()), 5)
        # 完成后追加录屏附件并置 ready
        update.assert_called_once()
        updates = update.call_args.args[1]
        self.assertEqual(updates['recording_status'], 'ready')
        self.assertEqual(
            updates['bug']['attachments'],
            ['/tmp/manual-log.zip', '/tmp/recent-minute.mp4'],
        )
        self.assertIn(
            '点击提交前 40 秒 + 点击提交后 20 秒录屏见附件',
            updates['preview']['description'],
        )

    def test_manual_log_bug_recording_failure_marks_failed(self):
        handler, response = self.make_handler()
        capture = FakeCapture()
        recorder = FakeRecorder()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(
                recorder, 'snapshot',
                side_effect=RuntimeError('record failed'),
            ),
            patch.object(
                server, 'save_draft', return_value=('manual-video', '/tmp/x')
            ),
            patch.object(server, 'update_bug') as update,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_session_bug({'assignee': '萌虎'})
            time.sleep(0.6)

        self.assertEqual(response['recording_status'], 'pending')
        update.assert_called_once()
        self.assertEqual(
            update.call_args.args[1]['recording_status'], 'failed'
        )

    def test_manual_log_bug_without_recorder_skips_thread(self):
        handler, response = self.make_handler()
        capture = FakeCapture()
        recorder = FakeRecorder()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(recorder, 'has_session', return_value=False),
            patch.object(
                server, 'save_draft', return_value=('manual-video', '/tmp/x')
            ) as save,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_session_bug({'assignee': '萌虎'})
            time.sleep(0.3)

        self.assertEqual(response['recording_status'], 'none')
        self.assertEqual(save.call_args.kwargs['recording_status'], 'none')
        self.assertEqual(recorder.snapshots, [])

    def test_confirm_rejects_pending_recording(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        errors = []
        handler._json = response.update
        handler._error = lambda message, status=500: errors.append(
            (message, status)
        )

        with patch.object(
            server, 'get_bug',
            return_value={'recording_status': 'pending'},
        ):
            handler._handle_bug_confirm('bug-1', {})

        self.assertTrue(errors)
        self.assertEqual(errors[0][1], 409)

    def test_screen_record_save_requires_recorder(self):
        handler = object.__new__(server.HuTestHandler)
        errors = []
        handler._error = lambda message, status=500: errors.append(
            (message, status)
        )
        recorder = FakeRecorder()
        with (
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(recorder, 'has_session', return_value=False),
        ):
            handler._handle_screen_record_save()
        self.assertTrue(errors)
        self.assertEqual(errors[0][1], 409)

    def test_screen_record_save_generates_and_serves_video(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        recorder = FakeRecorder()
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)

        with (
            patch.object(server, '_device_screen_recorder', recorder),
        ):
            handler._handle_screen_record_save()

        self.assertTrue(response.get('ok'))
        self.assertEqual(response.get('file'), 'recent-minute.mp4')
        self.assertIn('/api/uploads/', response.get('download', ''))
        self.assertEqual(recorder.anchors[0][0], 'session-1')
        anchor = recorder.anchors[0][1]
        self.assertIsNotNone(anchor)
        self.assertLess(abs(anchor - time.time()), 5)

    def test_screen_record_save_realtime_mode(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        recorder = FakeRecorder()
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)

        with (
            patch.object(server, '_device_screen_recorder', recorder),
        ):
            handler._handle_screen_record_save({'mode': 'realtime'})

        self.assertTrue(response.get('ok'))
        self.assertEqual(response.get('mode'), 'realtime')
        self.assertIsNone(recorder.anchors[0][1])
        self.assertEqual(recorder.snapshots[0][2], 60)
        self.assertEqual(recorder.snapshots[0][3], 0)

    def test_problem_bug_attaches_recent_recording_when_enabled(self):
        handler, response = self.make_handler()
        capture = FakeCapture()
        capture.problem_data.update({'recording_status': 'failed'})
        recorder = FakeRecorder()
        fake_bug = {
            'preview': {
                'description': (
                    '【问题摘要】\n完整带类型日志见附件，可下载查看。\n'
                    '录屏生成中，稍后自动附加'
                    '（问题前 60 秒 + 问题后 20 秒）。'
                ),
            },
            'bug': {'attachments': ['/tmp/full-log.zip']},
        }

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(
                server, 'save_draft', return_value=('problem-video', '/tmp/x')
            ) as save,
            patch.object(server, 'get_bug', return_value=fake_bug),
            patch.object(server, 'update_bug') as update,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_problem_bug('abc123', {'assignee': '萌虎'})
            time.sleep(1.5)

        self.assertEqual(save.call_args.kwargs['attachments'], [
            '/tmp/full-log.zip',
        ])
        self.assertEqual(response['recording_status'], 'pending')
        self.assertEqual(capture.marked, ('abc123', 'problem-video', ''))
        # 后台兜底生成录屏并附加，锚点是报错时刻
        update.assert_called_once()
        updates = update.call_args.args[1]
        self.assertEqual(updates['recording_status'], 'ready')
        self.assertEqual(
            updates['bug']['attachments'],
            ['/tmp/full-log.zip', '/tmp/recent-minute.mp4'],
        )
        self.assertEqual(recorder.anchors[0][0], 'session-1')
        self.assertEqual(recorder.anchors[0][1], 1234.5)

    def test_problem_bug_skips_recording_when_unavailable(self):
        """签名去重等标为 unavailable 的问题，提交 Bug 不应再尝试录屏。"""
        handler, response = self.make_handler()
        capture = FakeCapture()
        capture.problem_data.update({'recording_status': 'unavailable'})
        recorder = FakeRecorder()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(
                server, 'save_draft',
                return_value=('problem-norec', '/tmp/x'),
            ) as save,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_problem_bug('abc123', {'assignee': '萌虎'})

        self.assertEqual(response['recording_status'], 'none')
        self.assertEqual(recorder.snapshots, [])
        self.assertEqual(save.call_args.kwargs['recording_status'], 'none')

    def test_problem_bug_prefers_automatic_problem_recording(self):
        handler, response = self.make_handler()
        capture = FakeCapture()
        capture.problem_data.update({
            'recording_path': '/tmp/problem-moment.mp4',
            'recording_duration': 58,
            'recording_status': 'ready',
        })
        recorder = FakeRecorder()

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(server.os.path, 'isfile', return_value=True),
            patch.object(
                server, 'save_draft', return_value=('auto-video', '/tmp/x')
            ) as save,
            patch.object(server, 'log_activity'),
        ):
            handler._handle_log_problem_bug('abc123', {'assignee': '萌虎'})

        self.assertEqual(save.call_args.kwargs['attachments'], [
            '/tmp/full-log.zip', '/tmp/problem-moment.mp4',
        ])
        self.assertEqual(response['recording_status'], 'ready')
        self.assertEqual(save.call_args.kwargs['recording_status'], 'ready')
        self.assertEqual(recorder.snapshots, [])

    def test_bug_detail_exposes_attachment_names_and_sizes(self):
        handler, response = self.make_handler()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, 'device_logs_snapshot.zip')
            with open(log_path, 'wb') as handle:
                handle.write(b'12345')
            bug = {
                'bug': {'attachments': [log_path]},
                'survey_attachments': [{
                    'file_name': 'screen.png', 'file_token': 'token-1',
                }],
            }
            with patch.object(server, 'get_bug', return_value=bug):
                handler._handle_bug_get('bug-1')

        details = response['attachment_details']
        self.assertEqual(details[0]['name'], 'device_logs_snapshot.zip')
        self.assertEqual(details[0]['size'], 5)
        self.assertEqual(details[1]['name'], 'screen.png')
        self.assertEqual(details[1]['source'], 'survey')


if __name__ == '__main__':
    unittest.main()
