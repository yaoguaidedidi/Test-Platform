import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server
from claude_tasks import auto_submit
from lib import bug_queue, device_log_capture, feishu


class StaticPathTraversalTests(unittest.TestCase):
    def test_static_resolution_blocks_directory_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            css_path = os.path.join(tmp, 'app.css')
            with open(css_path, 'w', encoding='utf-8') as handle:
                handle.write('body {}')
            secret_path = os.path.join(os.path.dirname(tmp), 'secret.json')
            with open(secret_path, 'w', encoding='utf-8') as handle:
                handle.write('{}')

            with patch.object(server, 'STATIC_DIR', tmp):
                self.assertEqual(
                    server.resolve_static_file('/static/app.css'),
                    os.path.realpath(css_path),
                )
                self.assertIsNone(
                    server.resolve_static_file('/static/../secret.json')
                )


class ConfirmIdempotencyTests(unittest.TestCase):
    def _save_preview_ready(self):
        return bug_queue.save_draft(
            '重复确认测试',
            preview={
                'title': '重复确认测试', 'module': '', 'severity': '2',
                'priority': '1', 'description': '描述',
            },
        )

    def test_confirm_only_transitions_from_preview_ready_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                bug_id, _ = self._save_preview_ready()

                data, changed = bug_queue.confirm_bug(bug_id)
                self.assertTrue(changed)
                self.assertEqual(data['status'], 'confirmed')

                # 第二次确认不再流转（否则会再起一个提交进程）
                data, changed = bug_queue.confirm_bug(bug_id)
                self.assertFalse(changed)
                self.assertEqual(data['status'], 'confirmed')

    def test_confirm_does_not_touch_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                bug_id, _ = bug_queue.save_draft('未就绪草稿')
                data, changed = bug_queue.confirm_bug(bug_id)
                self.assertFalse(changed)
                self.assertEqual(data['status'], 'draft')

    def test_mark_submitted_still_works_after_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                bug_id, _ = self._save_preview_ready()
                bug_queue.confirm_bug(bug_id)
                result = bug_queue.mark_submitted(bug_id, '7056609999')
                self.assertEqual(result['status'], 'submitted')
                self.assertEqual(result['feishu_work_item_id'], '7056609999')

    def test_handle_bug_confirm_rejects_second_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                bug_id, _ = self._save_preview_ready()
                handler = object.__new__(server.HuTestHandler)
                responses = []
                errors = []
                handler._json = responses.append
                handler._error = lambda message, code=400: errors.append(
                    (message, code)
                )
                with (
                    patch.object(server, 'log_activity'),
                    patch.object(server.subprocess, 'Popen') as popen,
                ):
                    handler._handle_bug_confirm(bug_id, {})
                    handler._handle_bug_confirm(bug_id, {})

                self.assertEqual(
                    responses, [{'ok': True, 'status': 'confirmed'}]
                )
                self.assertEqual(errors[0][1], 409)
                popen.assert_called_once()


class SubmitIdempotencyTests(unittest.TestCase):
    def test_submit_skips_when_already_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = os.path.join(tmp, 'queue')
            os.makedirs(queue_dir)
            bug_id = 'already-submitted'
            with open(
                os.path.join(queue_dir, bug_id + '.json'),
                'w', encoding='utf-8',
            ) as handle:
                json.dump({
                    'id': bug_id, 'status': 'submitted',
                    'feishu_work_item_id': '7056601234',
                    'bug': {'assignee': '萌虎', 'attachments': []},
                }, handle)

            with (
                patch.object(auto_submit, 'QUEUE_DIR', queue_dir),
                patch.object(auto_submit, 'PENDING_DIR', os.path.join(tmp, 'pending')),
                patch.object(auto_submit, '_meegle_create_issue') as create,
                patch.object(auto_submit, 'log'),
            ):
                result = auto_submit.submit_bug(bug_id)

            self.assertFalse(result)
            create.assert_not_called()

    def test_submit_reuses_existing_work_item(self):
        """创建成功但附件校验失败后重试：已有 wid 也要继续走复用分支。"""
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = os.path.join(tmp, 'queue')
            os.makedirs(queue_dir)
            bug_id = 'retry-with-wid'
            with open(
                os.path.join(queue_dir, bug_id + '.json'),
                'w', encoding='utf-8',
            ) as handle:
                json.dump({
                    'id': bug_id, 'status': 'confirmed',
                    'feishu_work_item_id': '7056601234',
                    'bug': {'assignee': '萌虎', 'attachments': []},
                }, handle)

            with (
                patch.object(auto_submit, 'QUEUE_DIR', queue_dir),
                patch.object(auto_submit, 'PENDING_DIR', os.path.join(tmp, 'pending')),
                patch.object(
                    auto_submit, '_meegle_create_issue', return_value=True
                ) as create,
                patch.object(auto_submit, 'log'),
            ):
                result = auto_submit.submit_bug(bug_id)

            self.assertTrue(result)
            create.assert_called_once()

    def test_submit_skips_when_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = os.path.join(tmp, 'queue')
            os.makedirs(queue_dir)
            bug_id = 'still-draft'
            with open(
                os.path.join(queue_dir, bug_id + '.json'),
                'w', encoding='utf-8',
            ) as handle:
                json.dump({
                    'id': bug_id, 'status': 'draft',
                    'bug': {'assignee': '萌虎', 'attachments': []},
                }, handle)

            with (
                patch.object(auto_submit, 'QUEUE_DIR', queue_dir),
                patch.object(auto_submit, 'PENDING_DIR', os.path.join(tmp, 'pending')),
                patch.object(auto_submit, '_meegle_create_issue') as create,
                patch.object(auto_submit, 'log'),
            ):
                result = auto_submit.submit_bug(bug_id)

            self.assertFalse(result)
            create.assert_not_called()


class AttachmentDownloadTests(unittest.TestCase):
    def _fake_response(self, filename, content):
        return SimpleNamespace(
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
            content=content,
            raise_for_status=lambda: None,
        )

    def test_filename_is_sanitized_against_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(feishu, '_get_token', return_value='token'),
                patch.object(
                    feishu.requests, 'get',
                    return_value=self._fake_response('../../evil.png', b'x'),
                ),
            ):
                path = feishu.download_attachment('tok123', tmp)

            self.assertEqual(path, os.path.join(tmp, 'evil.png'))

    def test_same_name_attachments_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(feishu, '_get_token', return_value='token'),
                patch.object(
                    feishu.requests, 'get',
                    return_value=self._fake_response('shot.png', b'x'),
                ),
            ):
                first = feishu.download_attachment('token1111', tmp)
                second = feishu.download_attachment('token2222', tmp)

            self.assertNotEqual(first, second)
            self.assertTrue(os.path.isfile(first))
            self.assertTrue(os.path.isfile(second))


class BadParameterTests(unittest.TestCase):
    def test_lines_after_tolerates_non_numeric_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = device_log_capture.DeviceLogCapture(tmp)
            result = capture.lines_after('abc', 'xyz')
            self.assertTrue(result['ok'])
            self.assertEqual(result['lines'], [])
            self.assertEqual(result['cursor'], 0)

    def test_handle_generate_rejects_non_list_cases(self):
        for cases in ('not-a-list', None, 42):
            with self.subTest(cases=cases):
                handler = object.__new__(server.HuTestHandler)
                responses = []
                errors = []
                handler._json = responses.append
                handler._error = lambda message, code=400: errors.append(
                    (message, code)
                )
                handler._handle_generate({'title': 'x', 'cases': cases})
                self.assertEqual(responses, [])
                self.assertEqual(errors[0][1], 400)


class QueueRobustnessTests(unittest.TestCase):
    def test_list_queue_skips_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(
                os.path.join(tmp, '20260731_broken.json'),
                'w', encoding='utf-8',
            ) as handle:
                json.dump({
                    'id': '20260731_broken', 'status': 'draft',
                }, handle)  # 缺少 bug 字段，旧代码会 KeyError
            with open(
                os.path.join(tmp, '20260731_ok.json'),
                'w', encoding='utf-8',
            ) as handle:
                json.dump({
                    'id': '20260731_ok', 'status': 'draft', 'mode': 'detail',
                    'bug': {'brief_description': '正常', 'assignee': '萌虎'},
                }, handle)

            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                rows = bug_queue.list_queue()

        self.assertEqual([row['id'] for row in rows], ['20260731_ok'])


if __name__ == '__main__':
    unittest.main()
