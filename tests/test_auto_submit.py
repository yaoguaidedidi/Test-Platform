import json
import os
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from claude_tasks import auto_submit
from claude_tasks.auto_submit import _meegle_base_fields


class AutoSubmitFieldTests(unittest.TestCase):
    def test_reporter_uses_current_account_not_assignee(self):
        bug = {
            'bug': {'assignee': '其他经办人'},
            'preview': {
                'title': '标题', 'description': '描述',
                'severity': '2', 'priority': '1',
            },
        }

        with patch(
            'claude_tasks.auto_submit._resolve_user_key',
            return_value='assignee-user-key',
        ), patch(
            'claude_tasks.auto_submit._resolve_reporter_user_key',
            return_value='current-user-key',
        ):
            fields = _meegle_base_fields(bug)

        values = {item['field_key']: item['field_value'] for item in fields}
        self.assertEqual(
            json.loads(values['issue_operator']), ['assignee-user-key']
        )
        self.assertEqual(
            json.loads(values['issue_reporter']), ['current-user-key']
        )

    def test_new_issue_uploads_two_attachments_before_create(self):
        bug = {
            'id': 'bug-id', 'bug': {'assignee': '萌虎'},
            'preview': {
                'title': '标题', 'description': '描述',
                'severity': '2', 'priority': '1',
            },
        }
        attachments = [
            {'name': 'image.png', 'local_path': '/tmp/image.png'},
            {'name': 'video.mp4', 'local_path': '/tmp/video.mp4'},
        ]
        uploaded = [
            {'name': 'image.png', 'type': 'image/png', 'size': '10', 'fileToken': 'a'},
            {'name': 'video.mp4', 'type': 'video/mp4', 'size': '20', 'fileToken': 'b'},
        ]

        def run(args, timeout=180):
            if args[:2] == ['auth', 'status']:
                return SimpleNamespace(returncode=0, stdout='{}', stderr='')
            if args[:2] == ['workitem', 'create']:
                fields = json.loads(args[args.index('--fields') + 1])
                values = {item['field_key']: item['field_value'] for item in fields}
                self.assertEqual(
                    json.loads(values[auto_submit.ATTACHMENT_FIELD_KEY]), uploaded
                )
                return SimpleNamespace(
                    returncode=0, stdout='{"work_item_id":"7056609999"}', stderr=''
                )
            self.fail(f'意外命令: {args}')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bug.json')
            with (
                patch.object(auto_submit, '_run_meegle', side_effect=run),
                patch.object(auto_submit, '_resolve_user_key', return_value='assignee-key'),
                patch.object(auto_submit, '_resolve_reporter_user_key', return_value='reporter-key'),
                patch.object(auto_submit, '_meegle_upload_attachments', return_value=uploaded) as upload,
                patch.object(auto_submit, '_verify_meegle_attachments', return_value=True) as verify,
                patch.object(auto_submit, '_mark_submit_success', return_value=True) as success,
                patch.object(auto_submit, 'log'),
            ):
                result = auto_submit._meegle_create_issue(
                    'bug-id', path, bug, attachments
                )

        self.assertTrue(result)
        upload.assert_called_once_with(None, attachments)
        verify.assert_called_once_with('7056609999', attachments)
        success.assert_called_once()

    def test_create_scope_upload_uses_work_item_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'image.png')
            with open(path, 'wb') as handle:
                handle.write(b'png')
            response = SimpleNamespace(
                returncode=0,
                stdout='{"file_token":"token-1","file_url":"https://example.test/token-1"}',
                stderr='',
            )
            with (
                patch.object(auto_submit, '_run_meegle', return_value=response) as run,
                patch.object(auto_submit, 'log'),
            ):
                uploaded = auto_submit._meegle_upload_attachments(
                    None, [{'name': 'image.png', 'local_path': path}]
                )

        args = run.call_args.args[0]
        self.assertIn('--work-item-type', args)
        self.assertNotIn('--work-item-id', args)
        self.assertEqual(uploaded[0]['fileToken'], 'token-1')
        self.assertEqual(uploaded[0]['uid'], 'token-1')
        self.assertEqual(uploaded[0]['url'], 'https://example.test/token-1')

    def test_upload_attachments_parallel_and_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ('image.png', 'video.mp4'):
                path = os.path.join(tmp, name)
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 100)
                paths.append(path)
            attachments = [
                {'name': 'image.png', 'local_path': paths[0]},
                {'name': 'video.mp4', 'local_path': paths[1]},
            ]
            barrier = threading.Barrier(2)
            calls = []

            def run(args, timeout=180):
                calls.append(args)
                # 两个上传必须同时进行：串行实现会在这里超时抛异常
                barrier.wait(timeout=5)
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        '{"file_token":"tok","file_url":'
                        '"https://example.test/tok"}'
                    ),
                    stderr='',
                )

            with (
                patch.object(auto_submit, '_run_meegle', side_effect=run),
                patch.object(auto_submit, 'log'),
            ):
                uploaded = auto_submit._meegle_upload_attachments(
                    None, attachments
                )

        self.assertEqual(len(uploaded), 2)
        self.assertEqual(
            [item['name'] for item in uploaded],
            ['image.png', 'video.mp4'],
        )
        self.assertEqual(len(calls), 2)

    def test_upload_attachments_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ('a.log', 'b.mp4'):
                path = os.path.join(tmp, name)
                with open(path, 'wb') as handle:
                    handle.write(b'x')
                paths.append(path)
            attachments = [
                {'name': 'a.log', 'local_path': paths[0]},
                {'name': 'b.mp4', 'local_path': paths[1]},
            ]
            with (
                patch.object(
                    auto_submit, '_run_meegle',
                    return_value=SimpleNamespace(
                        returncode=1, stdout='', stderr='boom'
                    ),
                ),
                patch.object(auto_submit, 'log'),
            ):
                with self.assertRaises(RuntimeError):
                    auto_submit._meegle_upload_attachments(None, attachments)

    def test_existing_issue_with_manual_attachments_is_not_reuploaded(self):
        bug = {
            'id': 'bug-id', 'feishu_work_item_id': '7056604367',
            'bug': {'assignee': '萌虎'}, 'preview': {},
        }
        attachments = [
            {'name': 'image.png', 'local_path': '/tmp/image.png'},
            {'name': 'video.mp4', 'local_path': '/tmp/video.mp4'},
        ]
        status = SimpleNamespace(returncode=0, stdout='{}', stderr='')

        with (
            patch.object(auto_submit, '_run_meegle', return_value=status),
            patch.object(auto_submit, '_verify_meegle_attachments', return_value=True),
            patch.object(auto_submit, '_meegle_upload_attachments') as upload,
            patch.object(auto_submit, '_mark_submit_success', return_value=True) as success,
            patch.object(auto_submit, 'log'),
        ):
            result = auto_submit._meegle_create_issue(
                'bug-id', '/tmp/bug.json', bug, attachments
            )

        self.assertTrue(result)
        upload.assert_not_called()
        success.assert_called_once()

    def test_attachment_verification_accepts_manual_duplicate_suffix(self):
        with (
            patch.object(auto_submit, '_meegle_attachment_values', return_value=[
                {'name': 'image (4).png'}, {'name': 'video.mp4'},
            ]),
            patch.object(auto_submit, 'log'),
        ):
            result = auto_submit._verify_meegle_attachments(
                '7056604367', [{'name': 'image.png'}, {'name': 'video.mp4'}],
                attempts=1,
            )

        self.assertTrue(result)

    def test_meegle_failure_goes_to_retry_queue_without_any_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = os.path.join(tmp, 'queue')
            pending_dir = os.path.join(tmp, 'pending')
            os.makedirs(queue_dir)
            attachment_path = os.path.join(tmp, 'image.png')
            with open(attachment_path, 'wb') as handle:
                handle.write(b'png')
            bug_id = 'bug-id'
            bug_path = os.path.join(queue_dir, bug_id + '.json')
            with open(bug_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': bug_id, 'status': 'confirmed',
                    'survey_attachments': [],
                    'bug': {
                        'assignee': '萌虎', 'attachments': [attachment_path],
                    },
                    'preview': {},
                }, handle)

            def fail_meegle(_bug_id, _path, bug, _attachments):
                bug['error'] = '附件字段校验失败'
                return False

            with (
                patch.object(auto_submit, 'QUEUE_DIR', queue_dir),
                patch.object(auto_submit, 'PENDING_DIR', pending_dir),
                patch.object(auto_submit, '_meegle_create_issue', side_effect=fail_meegle),
                patch.object(auto_submit, 'log'),
            ):
                result = auto_submit.submit_bug(bug_id)

            with open(bug_path, encoding='utf-8') as handle:
                saved = json.load(handle)

        self.assertFalse(result)
        self.assertEqual(saved['status'], 'submission_failed')
        self.assertEqual(saved['error'], '附件字段校验失败')


if __name__ == '__main__':
    unittest.main()
