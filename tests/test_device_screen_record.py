import os
import json
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from lib.device_screen_record import DeviceScreenRecorder


class DeviceScreenRecorderTests(unittest.TestCase):
    def test_environment_requires_adb_and_ffmpeg_and_has_no_audio(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            'lib.device_screen_record.shutil.which',
            side_effect=lambda name: f'/bin/{name}' if name == 'adb' else None,
        ):
            environment = DeviceScreenRecorder(tmp).environment()

        self.assertFalse(environment['ready'])
        self.assertTrue(environment['adb'])
        self.assertFalse(environment['ffmpeg'])
        self.assertFalse(environment['audio'])

    def test_snapshot_merges_before_seconds_without_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60
            )
            session_id = '20260728_160000_deadbeef'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = []
            for index in range(1, 7):
                path = os.path.join(directory, f'segment-{index:05d}.mp4')
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 2048)
                segments.append({
                    'name': os.path.basename(path), 'path': path,
                    'duration': 15.0, 'size': 2048,
                })
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': segments,
                'snapshot_count': 0, 'last_snapshot': '',
            }
            observed = {}

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    observed['args'] = list(args)
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('lib.device_screen_record._run', side_effect=fake_run):
                output, info = recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id
                )

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 60)
            self.assertEqual(info['before_seconds'], 60)
            self.assertEqual(info['after_seconds'], 0)
            selected_inputs = [
                observed['args'][index + 1]
                for index, value in enumerate(observed['args'])
                if value == '-i'
            ]
            self.assertEqual(len(selected_inputs), 4)
            self.assertNotIn(segments[0]['path'], selected_inputs)
            self.assertNotIn(segments[1]['path'], selected_inputs)
            self.assertIn(segments[2]['path'], selected_inputs)
            self.assertIn(segments[3]['path'], selected_inputs)
            self.assertIn(segments[5]['path'], selected_inputs)
            self.assertIn('-an', observed['args'])
            self.assertIn('concat=n=4:v=1:a=0', observed['args'][
                observed['args'].index('-filter_complex') + 1
            ])
            self.assertEqual(recorder._session['snapshot_count'], 1)

    def test_snapshot_supports_window_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60
            )
            session_id = '20260810_150000_abcd1234'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = []
            for index in range(1, 7):
                path = os.path.join(directory, f'segment-{index:05d}.mp4')
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 2048)
                segments.append({
                    'name': os.path.basename(path), 'path': path,
                    'duration': 15.0, 'size': 2048,
                })
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': segments,
                'snapshot_count': 0, 'last_snapshot': '',
            }
            observed = {}

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    observed['args'] = list(args)
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('lib.device_screen_record._run', side_effect=fake_run):
                output, info = recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id,
                    before_seconds=60, after_seconds=0,
                )

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 60)
            self.assertEqual(info['before_seconds'], 60)
            self.assertEqual(info['after_seconds'], 0)
            selected_inputs = [
                observed['args'][index + 1]
                for index, value in enumerate(observed['args'])
                if value == '-i'
            ]
            self.assertEqual(len(selected_inputs), 4)

    def test_snapshot_waits_for_after_segments_when_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            session_id = '20260807_170000_active'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = []
            for index in range(1, 7):
                path = os.path.join(directory, f'segment-{index:05d}.mp4')
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 2048)
                segments.append({
                    'name': os.path.basename(path), 'path': path,
                    'duration': 15.0, 'size': 2048,
                })
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'running', 'segments': list(segments),
                'snapshot_count': 0, 'last_snapshot': '',
            }
            recorder._stop = threading.Event()

            class FakeThread:
                def is_alive(self):
                    return True

            recorder._thread = FakeThread()

            def add_after_segments():
                directory = recorder._session['id']
                with recorder._lock:
                    current = recorder._session['segments']
                    for index in range(7, 9):
                        path = os.path.join(
                            recorder.root, directory,
                            f'segment-{index:05d}.mp4',
                        )
                        with open(path, 'wb') as handle:
                            handle.write(b'x' * 2048)
                        current.append({
                            'name': os.path.basename(path), 'path': path,
                            'duration': 15.0, 'size': 2048,
                        })

            timer = threading.Timer(0.3, add_after_segments)
            timer.daemon = True
            timer.start()

            observed = {}

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    observed['args'] = list(args)
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            try:
                with (
                    patch('lib.device_screen_record._run', side_effect=fake_run),
                    patch.object(
                        recorder._segment_finished, 'wait', return_value=True,
                    ),
                    patch.object(
                        recorder, '_interrupt_screenrecord',
                        return_value=None,
                    ),
                ):
                    output, info = recorder.snapshot(
                        os.path.join(tmp, 'uploads'), session_id
                    )
            finally:
                timer.cancel()

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 60)
            self.assertEqual(info['before_seconds'], 40)
            self.assertEqual(info['after_seconds'], 20)
            selected_inputs = [
                observed['args'][index + 1]
                for index, value in enumerate(observed['args'])
                if value == '-i'
            ]
            self.assertEqual(len(selected_inputs), 5)
            self.assertIn(segments[5]['path'], selected_inputs)
            self.assertIn(
                os.path.join(recorder.root, session_id, 'segment-00007.mp4'),
                selected_inputs,
            )
            self.assertIn(
                os.path.join(recorder.root, session_id, 'segment-00008.mp4'),
                selected_inputs,
            )
            filter_complex = observed['args'][
                observed['args'].index('-filter_complex') + 1
            ]
            self.assertIn('concat=n=5:v=1:a=0', filter_complex)
            self.assertIn('trim=start=5.000:duration=60.000', filter_complex)

    def _make_window_segments(self, directory, count=6, duration=15.0, start=100.0):
        segments = []
        for index in range(1, count + 1):
            path = os.path.join(directory, f'segment-{index:05d}.mp4')
            with open(path, 'wb') as handle:
                handle.write(b'x' * 2048)
            segments.append({
                'name': os.path.basename(path), 'path': path,
                'duration': duration,
                'size': 2048,
                'start_epoch': start + (index - 1) * duration,
                'end_epoch': start + index * duration,
            })
        return segments

    def test_select_window_anchors_on_error_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            segments = self._make_window_segments(tmp)
            selected, skip, duration = recorder._select_window(
                segments, 160.0, 40, 20
            )
            self.assertEqual(len(selected), 5)
            self.assertNotIn(segments[0]['path'], [
                item['path'] for item in selected
            ])
            self.assertEqual(skip, 5.0)
            self.assertEqual(duration, 60.0)

    def test_select_window_truncated_when_data_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(tmp)
            segments = self._make_window_segments(
                tmp, count=2, start=100.0
            )
            selected, skip, duration = recorder._select_window(
                segments, 110.0, 40, 20
            )
            self.assertEqual(len(selected), 2)
            self.assertEqual(skip, 0.0)
            self.assertEqual(duration, 30.0)

    def test_snapshot_anchored_window_without_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            session_id = '20260807_180000_anchored'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = self._make_window_segments(directory)
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': list(segments),
                'snapshot_count': 0, 'last_snapshot': '',
            }
            observed = {}

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    observed['args'] = list(args)
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('lib.device_screen_record._run', side_effect=fake_run):
                output, info = recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id,
                    anchor_time=160.0,
                )

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 60)
            selected_inputs = [
                observed['args'][index + 1]
                for index, value in enumerate(observed['args'])
                if value == '-i'
            ]
            self.assertEqual(len(selected_inputs), 5)
            filter_complex = observed['args'][
                observed['args'].index('-filter_complex') + 1
            ]
            self.assertIn('concat=n=5:v=1:a=0', filter_complex)
            self.assertIn('trim=start=5.000:duration=60.000', filter_complex)

    def test_snapshot_anchor_falls_back_when_segments_have_no_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            session_id = '20260807_181000_legacy'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = []
            for index in range(1, 7):
                path = os.path.join(directory, f'segment-{index:05d}.mp4')
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 2048)
                segments.append({
                    'name': os.path.basename(path), 'path': path,
                    'duration': 15.0, 'size': 2048,
                })
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': list(segments),
                'snapshot_count': 0, 'last_snapshot': '',
            }
            observed = {}

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    observed['args'] = list(args)
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('lib.device_screen_record._run', side_effect=fake_run):
                output, info = recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id,
                    anchor_time=160.0,
                )

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 40)
            selected_inputs = [
                observed['args'][index + 1]
                for index, value in enumerate(observed['args'])
                if value == '-i'
            ]
            self.assertEqual(len(selected_inputs), 3)

    def test_snapshot_anchor_out_of_range_raises_instead_of_tail(self):
        """带时间戳的片段：锚点超出保留范围时不应回退成最近片段，
        否则生成的录屏与报错时刻无关，必须明确失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            session_id = '20260807_182000_oor'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = self._make_window_segments(
                directory, count=6, start=100.0
            )
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': list(segments),
                'snapshot_count': 0, 'last_snapshot': '',
            }
            # 片段覆盖 100~190s，锚点 300s 完全超出
            with self.assertRaisesRegex(
                RuntimeError, '超出循环录屏保留范围'
            ):
                recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id,
                    anchor_time=300.0,
                )

    def test_can_recover_window_matches_anchor_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=15, history_seconds=60,
                before_seconds=40, after_seconds=20,
            )
            session_id = '20260807_183000_recover'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segments = self._make_window_segments(
                directory, count=6, start=100.0
            )
            recorder._session = {
                'id': session_id, 'device_id': 'emulator-5554',
                'status': 'stopped', 'segments': list(segments),
                'snapshot_count': 0, 'last_snapshot': '',
            }
            recorder._write_meta()

            # 片段 100~190s，窗口 [130-40,130+20]=[90,150] 有重叠 → 可恢复
            self.assertTrue(recorder.can_recover_window(
                session_id, 130.0
            ))
            # 锚点 300s 窗口 [260,320] 与片段无重叠 → 不可恢复
            self.assertFalse(recorder.can_recover_window(
                session_id, 300.0
            ))
            # 无会话 → 不可恢复
            self.assertFalse(recorder.can_recover_window(
                'no-such-session', 130.0
            ))

    def test_snapshot_rejects_mismatched_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(tmp)
            recorder._session = {
                'id': 'session-1', 'segments': [],
                'snapshot_count': 0, 'last_snapshot': '',
            }
            with self.assertRaises(RuntimeError):
                recorder.snapshot(os.path.join(tmp, 'uploads'), 'session-2')

    def test_snapshot_loads_retained_session_after_server_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(tmp)
            session_id = 'session-retained'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            segment = os.path.join(directory, 'segment-00001.mp4')
            with open(segment, 'wb') as handle:
                handle.write(b'x' * 2048)
            metadata = {
                'id': session_id, 'status': 'stopped',
                'segments': [{
                    'name': 'segment-00001.mp4', 'path': segment,
                    'duration': 12.0, 'size': 2048,
                }],
                'snapshot_count': 0, 'last_snapshot': '',
            }
            with open(
                os.path.join(directory, 'metadata.json'), 'w',
                encoding='utf-8',
            ) as handle:
                json.dump(metadata, handle)

            def fake_run(args, timeout=15):
                if args[0] == 'ffmpeg':
                    with open(args[-1], 'wb') as handle:
                        handle.write(b'v' * 4096)
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            self.assertTrue(recorder.has_session(session_id))
            with patch('lib.device_screen_record._run', side_effect=fake_run):
                output, info = recorder.snapshot(
                    os.path.join(tmp, 'uploads'), session_id
                )

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(info['duration'], 12)
            self.assertIsNone(recorder.status()['session'])
            with open(
                os.path.join(directory, 'metadata.json'), encoding='utf-8'
            ) as handle:
                saved = json.load(handle)
            self.assertEqual(saved['snapshot_count'], 1)

    def test_trim_keeps_small_rolling_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(
                tmp, segment_seconds=10, history_seconds=60
            )
            segments = []
            for index in range(12):
                path = os.path.join(tmp, f'{index}.mp4')
                with open(path, 'wb') as handle:
                    handle.write(b'x')
                segments.append({'path': path, 'duration': 10})
            recorder._session = {'segments': segments}

            recorder._trim_segments()

            self.assertEqual(len(recorder._session['segments']), 8)
            self.assertFalse(os.path.exists(os.path.join(tmp, '0.mp4')))
            self.assertTrue(os.path.exists(os.path.join(tmp, '11.mp4')))

    def test_delete_session_removes_retained_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DeviceScreenRecorder(tmp)
            session_id = 'session-delete'
            directory = os.path.join(recorder.root, session_id)
            os.makedirs(directory)
            with open(os.path.join(directory, 'segment-00001.mp4'), 'wb') as handle:
                handle.write(b'video')
            recorder._session = {
                'id': session_id, 'status': 'stopped', 'segments': [],
            }

            recorder.delete_session(session_id)

            self.assertFalse(os.path.exists(directory))
            self.assertIsNone(recorder.status()['session'])


if __name__ == '__main__':
    unittest.main()
