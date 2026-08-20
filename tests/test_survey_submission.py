import json
import os
import tempfile
import unittest
from unittest.mock import mock_open, patch

import server
from claude_tasks import auto_process
from lib import bug_queue


class SurveySubmissionTests(unittest.TestCase):
    def test_builds_preview_from_original_survey_content(self):
        preview = bug_queue.build_survey_preview('问卷标题', '原始问题', 'iPhone 17')

        self.assertEqual(preview['title'], '问卷标题')
        self.assertEqual(preview['module'], '')
        self.assertEqual(preview['severity'], '2')
        self.assertEqual(preview['priority'], '1')
        self.assertEqual(
            preview['description'],
            '原始问题\n\n环境信息：iPhone 17\n\n（如截图录屏log所示）',
        )

    def test_survey_save_keeps_preview_and_does_not_start_analysis(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)
        preview = {
            'title': '人工标题', 'module': '', 'severity': '2',
            'priority': '1', 'description': '问卷原文',
        }

        with (
            patch.object(server, 'save_draft', return_value=('survey-id', '/tmp/x')) as save,
            patch.object(server, 'log_activity'),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_bug_save({
                'brief': '问卷标题', 'assignee': '萌虎', 'mode': 'survey',
                'survey_desc': '问卷原文', 'survey_env': '测试环境',
                'survey_attachments': [{'file_token': 'token-1'}],
                'preview': preview,
            })

        self.assertEqual(response['id'], 'survey-id')
        self.assertEqual(response['status'], 'preview_ready')
        self.assertEqual(save.call_args.kwargs['preview'], preview)
        popen.assert_not_called()

    def test_regular_save_still_starts_analysis(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)

        with (
            patch.object(server, 'save_draft', return_value=('bug-id', '/tmp/x')),
            patch.object(server, 'log_activity'),
            patch.object(server, 'open', mock_open()),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_bug_save({
                'brief': '普通 Bug', 'assignee': '萌虎', 'mode': 'detail',
            })

        self.assertEqual(response['status'], 'draft')
        popen.assert_called_once()

    def test_empty_or_too_short_brief_is_rejected_before_save(self):
        for payload in ({}, {'brief': ''}, {'brief': '   '}, {'brief': '卡'}):
            with self.subTest(payload=payload):
                handler = object.__new__(server.HuTestHandler)
                errors = []
                handler._json = lambda body: self.fail(f'不应成功: {body}')
                handler._error = lambda message, status=500: errors.append(
                    (message, status)
                )

                with (
                    patch.object(server, 'save_draft') as save,
                    patch.object(server.subprocess, 'Popen') as popen,
                ):
                    handler._handle_bug_save(payload)

                self.assertEqual(errors, [('简述至少需要 2 个字符', 400)])
                save.assert_not_called()
                popen.assert_not_called()

    def test_simple_save_builds_preview_without_starting_ai(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)

        with (
            patch.object(server, 'detect_module', return_value='图鉴系统'),
            patch.object(server, 'save_draft', return_value=('simple-id', '/tmp/x')) as save,
            patch.object(server, 'log_activity'),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_bug_save({
                'brief': '图鉴红点穿模', 'assignee': '萌虎', 'mode': 'simple',
            })

        preview = save.call_args.kwargs['preview']
        self.assertEqual(response['status'], 'preview_ready')
        self.assertEqual(preview['title'], '【图鉴系统】图鉴红点穿模')
        self.assertIn('如截图录屏log所示', preview['description'])
        popen.assert_not_called()

    def test_scripted_save_keeps_supplied_preview_without_starting_ai(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)
        preview = {
            'title': '【打点】参数缺失', 'module': '打点',
            'severity': '2', 'priority': '1', 'description': '固定报告',
        }

        with (
            patch.object(server, 'save_draft', return_value=('scripted-id', '/tmp/x')) as save,
            patch.object(server, 'log_activity'),
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_bug_save({
                'brief': preview['title'], 'mode': 'scripted',
                'preview': json.dumps(preview, ensure_ascii=False),
            })

        self.assertEqual(response['status'], 'preview_ready')
        self.assertEqual(save.call_args.kwargs['preview'], preview)
        popen.assert_not_called()

    def test_historical_survey_retry_is_deterministic(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        handler._error = lambda message, status=500: self.fail(message)
        old_bug = {
            'mode': 'survey', 'status': 'analysis_failed',
            'survey_desc': '原始问题', 'survey_env': '环境',
            'bug': {'brief_description': '问卷标题'},
        }

        with (
            patch.object(server, 'get_bug', return_value=old_bug),
            patch.object(server, 'update_bug') as update,
            patch.object(server.subprocess, 'Popen') as popen,
        ):
            handler._handle_bug_retry('survey-id')

        self.assertEqual(response['status'], 'preview_ready')
        self.assertEqual(update.call_args.args[1]['preview']['title'], '问卷标题')
        popen.assert_not_called()

    def test_auto_process_never_calls_ai_for_historical_survey(self):
        with tempfile.TemporaryDirectory() as tmp:
            bug_id = 'survey-old'
            path = os.path.join(tmp, bug_id + '.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': bug_id, 'status': 'draft', 'mode': 'survey',
                    'survey_desc': '原始问题', 'survey_env': '环境',
                    'bug': {'brief_description': '问卷标题', 'assignee': '萌虎'},
                }, handle, ensure_ascii=False)

            with (
                patch.object(auto_process, 'QUEUE_DIR', tmp),
                patch.object(auto_process.subprocess, 'run') as run,
                patch.object(auto_process, '_drive_search_test_cases') as search,
                patch('builtins.print'),
            ):
                auto_process.process_bug(bug_id)

            with open(path, encoding='utf-8') as handle:
                saved = json.load(handle)

        self.assertEqual(saved['status'], 'preview_ready')
        self.assertEqual(saved['preview']['description'], '原始问题\n\n环境信息：环境\n\n（如截图录屏log所示）')
        run.assert_not_called()
        search.assert_not_called()

    def test_auto_process_never_calls_ai_for_historical_simple_bug(self):
        with tempfile.TemporaryDirectory() as tmp:
            bug_id = 'simple-old'
            path = os.path.join(tmp, bug_id + '.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': bug_id, 'status': 'draft', 'mode': 'simple',
                    'bug': {
                        'brief_description': '红点穿模', 'assignee': '萌虎',
                        'module': '图鉴系统',
                    },
                }, handle, ensure_ascii=False)

            with (
                patch.object(auto_process, 'QUEUE_DIR', tmp),
                patch.object(auto_process.subprocess, 'run') as run,
                patch.object(auto_process, '_drive_search_test_cases') as search,
                patch('builtins.print'),
            ):
                auto_process.process_bug(bug_id)

            with open(path, encoding='utf-8') as handle:
                saved = json.load(handle)

        self.assertEqual(saved['status'], 'preview_ready')
        self.assertEqual(saved['preview']['title'], '【图鉴系统】红点穿模')
        run.assert_not_called()
        search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
