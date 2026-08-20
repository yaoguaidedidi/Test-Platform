import json
import os
import tempfile
import unittest
from unittest.mock import patch

from lib import bug_queue


class BugQueueListTests(unittest.TestCase):
    def test_list_queue_includes_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '20260723_example.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': '20260723_example',
                    'status': 'preview_ready',
                    'mode': 'scripted',
                    'created_at': '2026-07-23T12:00:00',
                    'bug': {
                        'brief_description': '打点参数缺失',
                        'assignee': '萌虎',
                        'module': '打点',
                    },
                }, handle, ensure_ascii=False)

            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                rows = bug_queue.list_queue()

        self.assertEqual(rows[0]['mode'], 'scripted')

    def test_list_queue_defaults_legacy_records_to_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '20260722_legacy.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'id': '20260722_legacy',
                    'status': 'submitted',
                    'bug': {
                        'brief_description': '旧记录',
                        'assignee': '萌虎',
                        'module': '',
                    },
                }, handle, ensure_ascii=False)

            with patch.object(bug_queue, 'QUEUE_DIR', tmp):
                rows = bug_queue.list_queue()

        self.assertEqual(rows[0]['mode'], 'detail')


if __name__ == '__main__':
    unittest.main()
