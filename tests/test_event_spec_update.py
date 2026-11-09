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
                    'title': server.EVENT_SPEC_SHEET_TITLES[0], 'sheetId': 'sheet-1'
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
                    'title': server.EVENT_SPEC_SHEET_TITLES[0], 'sheetId': 'sheet-1'
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
                        'title': server.EVENT_SPEC_SHEET_TITLES[0],
                        'sheetId': 'configured-sheet',
                    },
                ]},
                {'valueRange': {'values': [
                    ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型'],
                    ['se_test2', '会话变化', 'did', '设备ID', '设备唯一值', 'string'],
                ]}},
                {'valueRange': {'values': [
                    ['说明', '不是打点规范'],
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
                patch.object(server, 'EVENT_SPEC_SHEET_TITLES', ['新规范页']),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['sheet'], '新规范页')

    def test_auto_discovers_new_spec_sheet(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            header = ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型']
            responses = [
                {'sheets': [
                    {'title': '新手漏斗打点', 'sheetId': 'known'},
                    {'title': '旧版打点（废弃）', 'sheetId': 'deprecated'},
                    {'title': '缩写字典', 'sheetId': 'dictionary'},
                    {'title': '新手漏斗打点_c组', 'sheetId': 'new-group'},
                ]},
                {'valueRange': {'values': [
                    header,
                    ['first_open', '首次打开游戏', '', '', '', ''],
                ]}},
                {'valueRange': {'values': [
                    ['缩写规范', None, None],
                    ['chg_fr', '切换前场景'],
                ]}},
                {'valueRange': {'values': [
                    header,
                    ['tt_c_01_start', 'C组引导开始', '', '', '', ''],
                    ['tt_c_02_result', 'C组引导结果',
                     'result', '玩家操作结果', '1：成功；\n2：失败', 'int'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch.object(server, 'EVENT_SPEC_SHEET_TITLES', ['新手漏斗打点']),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['auto_sheets'], ['新手漏斗打点_c组'])
            self.assertIn('新手漏斗打点_c组', result['json']['sheet'])
            with open(
                os.path.join(data_dir, 'event_spec.json'), encoding='utf-8'
            ) as f:
                saved = json.load(f)
            self.assertIn('tt_c_02_result', saved['events'])
            self.assertEqual(
                saved['event_fields']['tt_c_02_result']['result']['type'],
                'INT',
            )
            self.assertNotIn('旧版打点（废弃）', result['json']['sheet'])

    def test_reads_each_configured_sheet_and_skips_its_header(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            header = ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型']
            responses = [
                {'sheets': [
                    {'title': '新手漏斗打点_a组', 'sheetId': 'group-a'},
                    {'title': '新手漏斗打点', 'sheetId': 'funnel'},
                ]},
                {'valueRange': {'values': [
                    header,
                    ['first_open', '首次打开游戏', '', '', '', ''],
                    ['', '', 'did', '设备ID', '设备唯一值', 'string'],
                ]}},
                {'valueRange': {'values': [
                    header,
                    ['tt_a_01_merge_generator_01_start', '生成器点击1-开始', '', '', '', ''],
                    ['tt_a_04_merge_gameplay_01_result', '玩家操作结果',
                     'result', '玩家合成结果', '1：合成成功', 'int'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch.object(
                    server,
                    'EVENT_SPEC_SHEET_TITLES',
                    ['新手漏斗打点', '新手漏斗打点_a组'],
                ),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertNotIn('error', result)
            self.assertEqual(result['json']['event_count'], 3)
            self.assertEqual(result['json']['sheet'], '新手漏斗打点、新手漏斗打点_a组')
            with open(
                os.path.join(data_dir, 'event_spec.json'), encoding='utf-8'
            ) as f:
                saved = json.load(f)
            self.assertIn('tt_a_01_merge_generator_01_start', saved['events'])
            self.assertEqual(
                list(saved['event_fields']['tt_a_04_merge_gameplay_01_result']),
                ['result'],
            )
            self.assertNotIn('result', saved['event_fields']['first_open'])

    def test_sheet_boundary_does_not_attach_orphan_field_to_previous_event(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            header = ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型']
            responses = [
                {'sheets': [
                    {'title': '页签一', 'sheetId': 'one'},
                    {'title': '页签二', 'sheetId': 'two'},
                ]},
                {'valueRange': {'values': [
                    header,
                    ['event_a', 'A', '', '', '', ''],
                ]}},
                {'valueRange': {'values': [
                    header,
                    ['', '', 'orphan_field', '无主字段', '', 'string'],
                    ['event_b', 'B', '', '', '', ''],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch.object(server, 'EVENT_SPEC_SHEET_TITLES', ['页签一', '页签二']),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/token'
                })

            self.assertNotIn('error', result)
            with open(
                os.path.join(data_dir, 'event_spec.json'), encoding='utf-8'
            ) as f:
                saved = json.load(f)
            self.assertNotIn('orphan_field', saved['event_fields']['event_a'])

    def test_same_field_with_different_types_is_saved_as_conflict(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            responses = [
                {'sheets': [{
                    'title': server.EVENT_SPEC_SHEET_TITLES[0], 'sheetId': 'sheet-1'
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

    def test_blank_timing_uses_remark_as_event_description(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, 'event_spec.json'), 'w', encoding='utf-8'
            ) as f:
                json.dump({'events': {}, 'fields': {}}, f)
            responses = [
                {'sheets': [{
                    'title': server.EVENT_SPEC_SHEET_TITLES[0], 'sheetId': 'sheet-1'
                }]},
                {'valueRange': {'values': [
                    ['事件名', '事件上报时机', '字段名', '说明', '取值', '类型', '', '备注'],
                    ['tt_b_05_go_merge_result', '', 'result', '玩家点击行为',
                     '1：点击Build按钮UI；\n2：点击其他位置', 'int', '',
                     '【可重复打点】玩家行为'],
                ]}},
            ]
            handler, result = self.make_handler()
            with (
                patch.object(server, 'DATA_DIR', data_dir),
                patch('lib.feishu._api', side_effect=responses),
            ):
                handler._handle_events_spec_update({
                    'url': 'https://example.feishu.cn/sheets/abc123'
                })

            self.assertNotIn('error', result)
            with open(
                os.path.join(data_dir, 'event_spec.json'), encoding='utf-8'
            ) as f:
                saved = json.load(f)
            self.assertEqual(
                saved['events']['tt_b_05_go_merge_result'],
                '【可重复打点】玩家行为',
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
