import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from lib.device_log_capture import DeviceLogCapture


def result(stdout='', stderr='', returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class DeviceLogCaptureTests(unittest.TestCase):
    def test_watch_app_alive_terminates_logcat_when_app_exits(self):
        """App 进程退出后应终止 logcat，让抓取循环能重新绑定新进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            capture._session = {
                'device_id': 'serial-1', 'app_id': 'com.example.game',
            }
            # 模拟 logcat 进程：一直运行，直到被 terminate
            fake_proc = subprocess.Popen(
                ['sleep', '30'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            outputs = {
                ('adb', '-s', 'serial-1', 'get-state'):
                    result('device'),
                ('adb', '-s', 'serial-1', 'shell', 'pidof',
                 'com.example.game'):
                    result(''),  # App 已退出
            }

            def fake_run(args, timeout=12):
                return outputs[tuple(args)]

            try:
                with patch(
                    'lib.device_log_capture._run', side_effect=fake_run
                ):
                    capture._watch_app_alive(fake_proc, 'com.example.game')
                self.assertIsNotNone(fake_proc.poll())
            finally:
                if fake_proc.poll() is None:
                    fake_proc.kill()

    def test_watch_app_alive_keeps_logcat_while_app_running(self):
        """App 运行时 logcat 不应被终止。"""
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            capture._session = {
                'device_id': 'serial-1', 'app_id': 'com.example.game',
            }
            fake_proc = subprocess.Popen(
                ['sleep', '30'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            outputs = {
                ('adb', '-s', 'serial-1', 'get-state'):
                    result('device'),
                ('adb', '-s', 'serial-1', 'shell', 'pidof',
                 'com.example.game'):
                    result('12345'),
            }

            def fake_run(args, timeout=12):
                return outputs[tuple(args)]

            try:
                with patch(
                    'lib.device_log_capture._run', side_effect=fake_run
                ):
                    t = threading.Thread(
                        target=capture._watch_app_alive,
                        args=(fake_proc, 'com.example.game'),
                        daemon=True,
                    )
                    t.start()
                    time.sleep(2.5)
                    capture._stop.set()
                    t.join(timeout=3)
                self.assertIsNone(fake_proc.poll())
            finally:
                if fake_proc.poll() is None:
                    fake_proc.kill()

    def test_parses_android_lines(self):
        android = DeviceLogCapture.parse_line(
            '07-22 12:01:02.123 123 456 E MyTag: failed', 'android'
        )
        self.assertEqual(android['level'], 'E')
        self.assertEqual(android['tag'], 'MyTag')
        self.assertEqual(android['message'], 'failed')

    def test_lists_android_devices_and_third_party_apps(self):
        outputs = {
            ('adb', 'devices', '-l'): result(
                'List of devices attached\nserial-1 device model:Pixel_9 device:tokay\n'
            ),
            ('adb', '-s', 'serial-1', 'shell', 'pm', 'list', 'packages', '-3'):
                result('package:com.example.game\npackage:com.example.tool\n'),
        }

        def fake_run(args, timeout=12):
            return outputs[tuple(args)]

        with tempfile.TemporaryDirectory() as tmp, \
            patch('lib.device_log_capture.shutil.which', side_effect=lambda name: '/bin/' + name if name == 'adb' else None), \
            patch('lib.device_log_capture._run', side_effect=fake_run):
            capture = DeviceLogCapture(tmp)
            devices = capture.devices()['devices']
            apps = capture.apps('android', 'serial-1')['apps']

        self.assertEqual(devices[0]['name'], 'Pixel_9')
        self.assertEqual([app['id'] for app in apps], [
            'com.example.game', 'com.example.tool'
        ])

    def test_rotates_parts_and_keeps_incremental_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp, part_size=12)
            session_id = '20260722_120000_deadbeef'
            os.makedirs(os.path.join(capture.root, session_id))
            capture._session = {
                'id': session_id, 'platform': 'android', 'parts': [],
                'size': 0, 'line_count': 0,
            }
            capture._append('1234567890')
            capture._append('abcdefghij')

            self.assertEqual(len(capture._session['parts']), 2)
            self.assertEqual(capture._session['size'], 22)
            rows = capture.lines_after(0)['lines']
            self.assertEqual([row['cursor'] for row in rows], [1, 2])

    def test_discards_noise_lines_but_keeps_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            session_id = '20260722_120000_deadbeef'
            os.makedirs(os.path.join(capture.root, session_id))
            capture._session = {
                'id': session_id, 'platform': 'android', 'parts': [],
                'size': 0, 'line_count': 0,
            }
            noise_lines = [
                '08-11 12:00:00.001 11617 11670 I MESA    : '
                'exportSyncFdForQSRILocked: call for image 0xb40000760695f1d0 '
                'hos timage handle 0x70022000036ca',
                '08-11 12:00:00.002 11617 11670 I OpenGLRenderer: frame done',
                '08-11 12:00:00.003 11617 11670 I Unity   : '
                'Background concurrent copying GC freed 100',
                '08-11 12:00:00.004 11617 11670 W Unity   : '
                'WARNING: RGBA Compressed ASTC4X4 texture format is not '
                'supported, decompressing texture',
                '08-11 12:00:00.005 11617 11670 I Choreographer: '
                'app_time_stats: total=10',
            ]
            keep_lines = [
                '08-11 12:00:00.006 11617 11633 I Unity   : '
                'DM.Hotfix.Match.ColorballRocketCrush:CreateRocket('
                'PieceModel, CellModel)',
                '08-11 12:00:00.007 11617 11633 I Unity   : '
                '(Filename: ./Assets/Editor/ReleaseTools/ReleaseTools.cs '
                'Line: 450)',
            ]
            for line in noise_lines + keep_lines:
                capture._append(line)

            self.assertEqual(capture._session['line_count'], len(keep_lines))
            rows = capture.lines_after(0)['lines']
            self.assertEqual(len(rows), len(keep_lines))
            content = ''
            for part in capture._session['parts']:
                path = os.path.join(
                    capture.root, session_id, part['name']
                )
                with open(path, encoding='utf-8', errors='replace') as handle:
                    content += handle.read()
            self.assertNotIn('exportSyncFdForQSRILocked', content)
            self.assertNotIn('app_time_stats', content)
            self.assertIn('CreateRocket', content)
            self.assertIn('(Filename:', content)

    def test_trims_oldest_parts_over_max_total_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(
                tmp, part_size=10, max_total_size=15
            )
            session_id = '20260722_120000_deadbeef'
            os.makedirs(os.path.join(capture.root, session_id))
            capture._session = {
                'id': session_id, 'platform': 'android', 'parts': [],
                'size': 0, 'line_count': 0,
            }
            for _ in range(6):
                capture._append('1234567890')

            self.assertEqual(len(capture._session['parts']), 1)
            self.assertLessEqual(capture._session['size'], 15)
            files = [
                name for name in os.listdir(
                    os.path.join(capture.root, session_id)
                ) if name.startswith('part-')
            ]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0], capture._session['parts'][0]['name'])

    def test_rejects_ios_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            with self.assertRaises(ValueError):
                capture.start('ios', 'phone-1', 'com.example.game')

    def test_archives_and_deletes_finished_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            session_id = '20260722_120000_deadbeef'
            session_dir = os.path.join(capture.root, session_id)
            os.makedirs(session_dir)
            with open(os.path.join(session_dir, 'metadata.json'), 'w') as handle:
                json.dump({'id': session_id, 'size': 3}, handle)
            with open(os.path.join(session_dir, 'part-001.log'), 'w') as handle:
                handle.write('log')
            capture._session = {'id': session_id, 'status': 'stopped'}

            zip_path = capture.session_zip(session_id)
            self.assertTrue(os.path.isfile(zip_path))
            with zipfile.ZipFile(zip_path) as archive:
                self.assertIn('带类型的完整日志.log', archive.namelist())
            capture.delete_session(session_id)
            self.assertFalse(os.path.exists(session_dir))
            self.assertFalse(os.path.exists(zip_path))
            self.assertIsNone(capture.status()['session'])

    def test_clears_finished_sessions_but_keeps_active_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            active_id = '20260722_120000_active01'
            old_ids = [
                '20260721_120000_history1',
                '20260720_120000_history2',
            ]
            for session_id in [active_id] + old_ids:
                session_dir = os.path.join(capture.root, session_id)
                os.makedirs(session_dir)
                with open(os.path.join(session_dir, 'metadata.json'), 'w') as handle:
                    json.dump({'id': session_id, 'size': 3}, handle)
            capture._session = {'id': active_id, 'status': 'running'}
            capture._thread = SimpleNamespace(is_alive=lambda: True)

            result = capture.clear_sessions()

            self.assertEqual(result['deleted_count'], 2)
            self.assertTrue(os.path.isdir(os.path.join(capture.root, active_id)))
            for session_id in old_ids:
                self.assertFalse(os.path.exists(os.path.join(capture.root, session_id)))

    def test_rejects_unsafe_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            with self.assertRaises(ValueError):
                capture.apps('android', '../../bad')

    def _capture_with_session(self, tmp):
        capture = DeviceLogCapture(tmp)
        session_id = '20260722_120000_deadbeef'
        os.makedirs(os.path.join(capture.root, session_id))
        capture._session = {
            'id': session_id, 'platform': 'android', 'device_id': 'pixel',
            'app_id': 'com.example.game', 'app_name': 'Example',
            'parts': [], 'size': 0, 'line_count': 0, 'problem_count': 0,
        }
        return capture

    def test_groups_duplicate_errors_and_ignores_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            capture._append(
                '07-22 12:01:01.100 123 456 W Unity: retry later'
            )
            capture._append(
                '07-22 12:01:02.100 123 456 E Unity: request 41 failed'
            )
            capture._append(
                '07-22 12:01:03.100 123 456 E Unity: request 42 failed'
            )

            problems = capture.problems()['problems']
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0]['count'], 2)
            self.assertEqual(problems[0]['kind'], 'error')

    def test_tracks_last_error_with_computer_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            capture._append(
                '07-22 12:01:01.100 123 456 I Unity: before'
            )
            capture._append(
                '07-22 12:01:02.100 123 456 E Unity: boom failed'
            )
            capture._append(
                '07-22 12:01:03.100 123 456 W Unity: later'
            )

            self.assertGreater(capture._session['last_error_at'], 0)
            problems = capture.problems()['problems']
            self.assertGreater(problems[0]['first_epoch'], 0)
            self.assertGreater(problems[0]['last_epoch'], 0)
            self.assertLessEqual(
                problems[0]['first_epoch'], problems[0]['last_epoch']
            )

    def test_notifies_once_when_a_new_problem_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            notified = []
            capture = DeviceLogCapture(tmp, on_problem=notified.append)
            session_id = '20260722_120000_deadbeef'
            os.makedirs(os.path.join(capture.root, session_id))
            capture._session = {
                'id': session_id, 'platform': 'android',
                'parts': [], 'size': 0, 'line_count': 0,
                'problem_count': 0,
            }
            capture._append(
                '07-22 12:01:02.100 123 456 E Unity: request 41 failed'
            )
            capture._append(
                '07-22 12:01:03.100 123 456 E Unity: request 42 failed'
            )

            self.assertEqual(len(notified), 1)
            self.assertEqual(notified[0]['session_id'], session_id)
            self.assertEqual(notified[0]['recording_status'], 'pending')

    def test_detects_crash_network_and_unity_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            capture._append(
                '07-22 12:01:01.100 123 456 F AndroidRuntime: FATAL EXCEPTION: main'
            )
            capture._append(
                '07-22 12:01:02.100 123 456 I Unity: HTTP 503 request failed'
            )
            capture._append(
                '07-22 12:01:03.100 123 456 I Unity: NullReferenceException: missing'
            )

            kinds = {row['kind'] for row in capture.problems()['problems']}
            self.assertEqual(kinds, {'crash', 'network', 'exception'})

    def test_problem_context_and_snapshot_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            capture._append(
                '07-22 12:01:01.100 123 456 I Unity: before'
            )
            capture._append(
                '07-22 12:01:02.100 123 456 E Unity: boom failed'
            )
            problem_id = capture.problems()['problems'][0]['id']
            capture._append(
                '07-22 12:01:03.100 123 456 I Unity: after'
            )

            detail = capture.problem(problem_id)
            self.assertEqual(
                [row['message'] for row in detail['context']],
                ['before', 'boom failed', 'after'],
            )
            with tempfile.TemporaryDirectory() as output:
                snapshot, _ = capture.problem_snapshot(problem_id, output)
                capture._append(
                    '07-22 12:01:04.100 123 456 I Unity: later'
                )
                with open(snapshot) as handle:
                    raw = handle.read()
            self.assertTrue(snapshot.endswith('.log'))
            self.assertIn('[ERROR]', raw)
            self.assertIn('boom failed', raw)
            self.assertNotIn('later', raw)

    def test_filters_known_render_noise_and_unity_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            capture._append(
                '07-22 12:01:01.100 123 456 E EGL_emulation: failed to render'
            )
            capture._append(
                '07-22 12:01:02.100 123 456 E Unity: Foo.Bar:Baz()'
            )
            capture._append(
                '07-22 12:01:03.100 123 456 I Unity: '
                '07-22 12:01:03.100 123 456 W Unity :'
            )
            self.assertEqual(capture.problems()['problems'], [])

    def test_filters_android_art_gc_without_hiding_business_gc_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            variants = [
                'Background concurrent mark compact GC freed 2348KB AllocSpace bytes',
                'Background concurrent copying GC freed 1024KB AllocSpace bytes',
                'Background young concurrent copying GC freed 512KB AllocSpace bytes',
                'Concurrent copying GC freed 768KB AllocSpace bytes',
                'Explicit concurrent mark compact GC freed 256KB AllocSpace bytes',
            ]
            for message in variants:
                with self.subTest(message=message):
                    self.assertTrue(capture._is_noise({
                        'process': '2174', 'tag': 'com.funark.dream',
                        'message': message,
                    }))

            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'Business GC freed reward cache for next level',
            }))
            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'OutOfMemoryError after GC failed',
            }))

    def test_filters_unity_astc_fallback_without_hiding_other_texture_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            for message in (
                'WARNING: RGBA Compressed ASTC5X5 UNorm format is not supported, '
                'decompressing texture',
                'WARNING: RGB Compressed ASTC6X6 SRGB format is not supported, '
                'decompressing texture',
                'WARNING: RGBA Compressed ASTC4X4 UNorm format is not supported, '
                'decompressing texture',
            ):
                with self.subTest(message=message):
                    self.assertTrue(capture._is_noise({
                        'process': '2174', 'tag': 'Unity',
                        'message': message,
                    }))

            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'WARNING: texture decompression failed: out of memory',
            }))
            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'WARNING: ASTC texture is corrupted',
            }))

    def test_filters_emulator_mesa_qsri_spam_without_hiding_other_mesa_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            for message in (
                'exportSyncFdForQSRILocked: call for image 0xb40000770f767890 '
                'hos timage handle 0x7000400000660',
                'exportSyncFdForQSRILocked: got fd: 169',
            ):
                with self.subTest(message=message):
                    self.assertTrue(capture._is_noise({
                        'process': '3260', 'tag': 'MESA', 'message': message,
                    }))

            self.assertFalse(capture._is_noise({
                'process': '3260', 'tag': 'MESA',
                'message': 'exportSyncFdForQSRILocked: failed to export image',
            }))
            self.assertFalse(capture._is_noise({
                'process': '3260', 'tag': 'Unity',
                'message': 'exportSyncFdForQSRILocked: got fd: 169',
            }))

    def test_filters_unity_unused_asset_details_without_hiding_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = DeviceLogCapture(tmp)
            details = (
                'Unloading 0 Unused Serialized files '
                '(Serialized files now loaded: 163)',
                'Unloading 0 unused Assets / (0 B). '
                'Loaded Objects now: 23407.',
                'Memory consumption went from 176.6 MB to 176.6 MB.',
                'Total: 220.332625 ms (FindLiveObjects: 24.504167 ms '
                'CreateObjectMapping: 1.882375 ms MarkObjects: 193.860417 ms '
                'DeleteObjects: 0.084708 ms)',
            )
            for message in details:
                with self.subTest(message=message):
                    self.assertTrue(capture._is_noise({
                        'process': '2174', 'tag': 'Unity',
                        'message': message,
                    }))

            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'Failed to unload unused assets: out of memory',
            }))
            self.assertFalse(capture._is_noise({
                'process': '2174', 'tag': 'Unity',
                'message': 'Memory consumption exceeded safe threshold',
            }))

    def test_manual_session_snapshot_requires_logs_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture_with_session(tmp)
            with self.assertRaises(RuntimeError):
                capture.session_snapshot(os.path.join(tmp, 'uploads'))
            capture._append(
                '07-22 12:01:01.100 123 456 I Unity: captured'
            )
            snapshot, session = capture.session_snapshot(
                os.path.join(tmp, 'uploads')
            )
            capture._append(
                '07-22 12:01:02.100 123 456 I Unity: later'
            )
            with open(snapshot) as handle:
                raw = handle.read()
            self.assertEqual(session['line_count'], 1)
            self.assertTrue(snapshot.endswith('.log'))
            self.assertIn('captured', raw)
            self.assertNotIn('later', raw)


if __name__ == '__main__':
    unittest.main()
