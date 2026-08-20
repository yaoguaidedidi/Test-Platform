import unittest
from unittest.mock import patch

import server


class FakeCapture:
    def __init__(self):
        self.marks = []

    def mark_problem_recording(
        self, problem_id, path='', duration=0, error='', session_id='',
        status=None,
    ):
        self.marks.append({
            'problem_id': problem_id, 'path': path, 'duration': duration,
            'error': error, 'session_id': session_id, 'status': status,
        })


class FakeRecorder:
    before_seconds = 60
    after_seconds = 20

    def __init__(self):
        self.snapshots = []

    def snapshot(self, output_dir, session_id='', anchor_time=None,
                 before_seconds=None, after_seconds=None):
        self.snapshots.append((session_id, anchor_time))
        return '/tmp/shared.mp4', {'duration': 80}


class FakeThread:
    instances = []

    def __init__(self, target=None, args=(), kwargs=None, name='',
                 daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        FakeThread.instances.append(self)


class ProblemRecordingWindowTests(unittest.TestCase):
    def setUp(self):
        FakeThread.instances = []
        server._problem_recording_recent = None
        server._problem_recording_busy = 0
        server._problem_recording_queue = []

    def make_env(self):
        capture = FakeCapture()
        recorder = FakeRecorder()
        return capture, recorder

    def problem(self, pid, anchor, session_id='s1'):
        return {
            'id': pid, 'session_id': session_id, 'last_epoch': anchor,
        }

    def test_window_inside_marks_pending_without_new_thread(self):
        capture, recorder = self.make_env()
        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(server.threading, 'Thread', FakeThread),
        ):
            server._schedule_problem_recording(self.problem('p1', 1000))
            server._schedule_problem_recording(self.problem('p2', 1010))

        self.assertEqual(len(FakeThread.instances), 1)
        pending_marks = [
            m for m in capture.marks
            if m['problem_id'] == 'p2' and m['status'] == 'pending'
        ]
        self.assertEqual(len(pending_marks), 1)

    def test_window_outside_queues_new_window(self):
        capture, recorder = self.make_env()
        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(server.threading, 'Thread', FakeThread),
        ):
            server._schedule_problem_recording(self.problem('p1', 1000))
            # 1100 在 [940, 1020] 之外，但已有冻结任务在跑 → 入队不新起线程
            server._schedule_problem_recording(self.problem('p2', 1100))
            self.assertEqual(len(FakeThread.instances), 1)
            self.assertEqual(len(server._problem_recording_queue), 1)

            # 模拟第一个窗口冻结完成，应自动启动队列里的下一个窗口
            FakeThread.instances[0].target(*FakeThread.instances[0].args)

        self.assertEqual(len(FakeThread.instances), 2)

    def test_window_freeze_marks_all_problems_shared(self):
        capture, recorder = self.make_env()
        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(server.threading, 'Thread', FakeThread),
        ):
            server._schedule_problem_recording(self.problem('p1', 1000))
            server._schedule_problem_recording(self.problem('p2', 1010))
            FakeThread.instances[0].target(*FakeThread.instances[0].args)

        ready = [m for m in capture.marks if m['status'] is None]
        self.assertEqual(len(ready), 2)
        self.assertEqual(
            {m['path'] for m in ready}, {'/tmp/shared.mp4'}
        )
        self.assertEqual(recorder.snapshots, [('s1', 1000.0)])

    def test_window_freeze_failure_marks_all_failed(self):
        capture, recorder = self.make_env()

        def fail_snapshot(*args, **kwargs):
            raise RuntimeError('record fail')

        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(recorder, 'snapshot', side_effect=fail_snapshot),
            patch.object(server.threading, 'Thread', FakeThread),
        ):
            server._schedule_problem_recording(self.problem('p1', 1000))
            server._schedule_problem_recording(self.problem('p2', 1010))
            FakeThread.instances[0].target(*FakeThread.instances[0].args)

        marks = [
            m for m in capture.marks
            if m['path'] == '' and m['status'] is None
        ]
        self.assertEqual(len(marks), 2)
        self.assertTrue(all('record fail' in m['error'] for m in marks))

    def test_finished_window_reuses_recording_directly(self):
        capture, recorder = self.make_env()
        with (
            patch.object(server, '_device_log_capture', capture),
            patch.object(server, '_device_screen_recorder', recorder),
            patch.object(server.threading, 'Thread', FakeThread),
        ):
            server._schedule_problem_recording(self.problem('p1', 1000))
            FakeThread.instances[0].target(*FakeThread.instances[0].args)
            # 窗口已冻结完成，稍后窗口内报错直接复用，不再新起线程
            server._schedule_problem_recording(self.problem('p3', 1015))

        self.assertEqual(len(FakeThread.instances), 1)
        reused = [m for m in capture.marks if m['problem_id'] == 'p3']
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]['path'], '/tmp/shared.mp4')
        self.assertIsNone(reused[0]['status'])


if __name__ == '__main__':
    unittest.main()
