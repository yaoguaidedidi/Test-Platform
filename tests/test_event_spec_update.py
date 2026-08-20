import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server


class EventSpecUpdateTests(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(server.HuTestHandler)
        result = {}
        handler._json = lambda payload: result.update({'json': payload})
        handler._error = lambda message, status=400: result.update({
            'error': message, 'status': status
        })
        return handler, result

    def test_update_preserves_events_and_existing_field_types(self):
        with tempfile.TemporaryDirectory() as data_dir:
            spec_path = os.path.join(data_dir, 'event_spec.json')
            with open(spec_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'events': {'se_test2': '会话事件'},
                    'fields': {'seq_id': {'type': 'INT', 'desc': '旧说明'}},
                }, f, ensure_ascii=False)

            responses = [
                {'sheets': [{
                    'title': server.EVENT_SPEC_SHEET_TITLE, 'sheetId': 'sheet-1'
                }]},
                {'valueRange': {'values': [
                    ['A', 'B', '字段名', '说明'],
                    ['', '', 'seq_id', '新的事件序号说明'],
                    ['', '', 'did', '设备ID'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch('lib.feishu._api', side_effect=responses) as api,
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/abc123?sheet=sheet-1'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['count'], 2)
            self.assertEqual(result['json']['events_preserved'], 1)
            self.assertEqual(
                api.call_args_list[0].args[1],
                '/sheets/v2/spreadsheets/abc123/metainfo',
            )
            with open(spec_path, encoding='utf-8') as f:
                updated = json.load(f)
            self.assertEqual(updated['events'], {'se_test2': '会话事件'})
            self.assertEqual(updated['fields']['seq_id']['type'], 'INT')
            self.assertEqual(updated['fields']['seq_id']['desc'], '新的事件序号说明')
            self.assertEqual(updated['event_fields'], {})

    def test_empty_sheet_does_not_overwrite_existing_spec(self):
        with tempfile.TemporaryDirectory() as data_dir:
            spec_path = os.path.join(data_dir, 'event_spec.json')
            original = {'events': {'keep': '保留'}, 'fields': {'x': {'desc': 'x'}}}
            with open(spec_path, 'w', encoding='utf-8') as f:
                json.dump(original, f, ensure_ascii=False)

            responses = [
                {'sheets': [{
                    'title': server.EVENT_SPEC_SHEET_TITLE, 'sheetId': 'sheet-1'
                }]},
                {'valueRange': {'values': [['A', 'B', '字段名', '说明']]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/abc123'
                })

            self.assertEqual(result['status'], 400)
            with open(spec_path, encoding='utf-8') as f:
                self.assertEqual(json.load(f), original)

    def test_wiki_sheet_link_resolves_to_sheet_token(self):
        with tempfile.TemporaryDirectory() as data_dir:
            spec_path = os.path.join(data_dir, 'event_spec.json')
            with open(spec_path, 'w', encoding='utf-8') as f:
                json.dump({'events': {'keep': '保留'}, 'fields': {}}, f)

            responses = [
                {'node': {'obj_type': 'sheet', 'obj_token': 'sheet-token'}},
                {'sheets': [
                    {'title': '其他页签', 'sheetId': 'wrong-sheet'},
                    {
                        'title': server.EVENT_SPEC_SHEET_TITLE,
                        'sheetId': 'configured-sheet',
                    },
                ]},
                {'valueRange': {'values': [
                    ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型'],
                    ['se_test2', '会话变化', 'did', '设备ID', '设备唯一值', 'string'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch('lib.feishu._api', side_effect=responses) as api,
            ):
                handler._handle_events_spec_update({
                    'url': 'https://funarkgame.feishu.cn/wiki/wiki-token?sheet=wrong-sheet'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['event_count'], 1)
            with open(spec_path, encoding='utf-8') as f:
                updated = json.load(f)
            self.assertEqual(
                updated['event_fields']['se_test2']['did'],
                {
                    'type': 'STRING',
                    'desc': '设备ID',
                    'value_desc': '设备唯一值',
                },
            )
            self.assertEqual(
                api.call_args_list[0].args,
                ('GET', '/wiki/v2/spaces/get_node'),
            )
            self.assertEqual(
                api.call_args_list[0].kwargs,
                {'params': {'token': 'wiki-token'}},
            )
            self.assertEqual(
                api.call_args_list[1].args[1],
                '/sheets/v2/spreadsheets/sheet-token/metainfo',
            )
            self.assertEqual(
                api.call_args_list[2].args[1],
                '/sheets/v2/spreadsheets/sheet-token/values/configured-sheet',
            )

    def test_sheet_title_can_be_overridden_through_config_port(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            responses = [
                {'sheets': [{'title': '新规范页', 'sheetId': 'new-sheet'}]},
                {'valueRange': {'values': [
                    ['事件名', '事件上报时机', '字段名', '说明'],
                    ['event', '上报', 'did', '设备ID'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch.object(server, 'EVENT_SPEC_SHEET_TITLE', '新规范页'),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['sheet'], '新规范页')

    def test_same_field_with_different_types_is_saved_as_conflict(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            responses = [
                {'sheets': [{
                    'title': server.EVENT_SPEC_SHEET_TITLE, 'sheetId': 'sheet-1'
                }]},
                {'valueRange': {'values': [
                    ['事件名', '说明', '字段名', '字段说明', '取值', '类型'],
                    ['event_a', 'A', 'shared_id', '共享字段', '', 'string'],
                    ['event_b', 'B', 'shared_id', '共享字段', '', 'int'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertEqual(result['json']['conflict_count'], 1)
            conflict = result['json']['consistency_issues'][0]
            self.assertEqual(conflict['field'], 'shared_id')
            self.assertEqual(
                conflict['events'],
                [
                    {'event': 'event_a', 'type': 'STRING'},
                    {'event': 'event_b', 'type': 'INT'},
                ],
            )
            with open(
                os.path.join(data_dir, 'event_spec.json'), encoding='utf-8'
            ) as f:
                saved = json.load(f)
            self.assertEqual(saved['consistency_issues'], [conflict])

    def test_same_field_with_same_type_has_no_conflict(self):
        event_fields = {
            'event_a': {'shared_id': {'type': 'string'}},
            'event_b': {'shared_id': {'type': 'STRING'}},
        }
        self.assertEqual(
            server.find_event_field_type_conflicts(event_fields),
            [],
        )

    def test_expired_auth_returns_actionable_message(self):
        handler, result = self.make_handler()
        with patch(
            'lib.feishu._api',
            side_effect=Exception('code=20026 refresh token not found'),
        ):
            handler._handle_events_spec_update({
                'url': 'https://funarkgame.feishu.cn/wiki/wiki-token'
            })

        # 409 表示飞书业务凭证过期，与平台登录未登录(401)区分，
        # 避免前端误弹登录遮罩。
        self.assertEqual(result['status'], 409)
        self.assertIn('重新授权', result['error'])


if __name__ == '__main__':
    unittest.main()
