import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lib.event_tracker import (EventTracker, _parse_event, analyze_events,
                               analyze_relations, describe_rules, RELATION_RULES)


class EventTrackerTests(unittest.TestCase):
    def _read(self, payload):
        tracker = EventTracker()
        tracker._running = True
        lines = [
            '[ThinkingData] Info: Enqueue data(test):\n',
            *[line + '\n' for line in json.dumps(payload, indent=2).splitlines()],
        ]
        tracker._process = SimpleNamespace(
            stdout=iter(lines), poll=lambda: 0, wait=lambda timeout=None: 0
        )
        tracker._read_loop()
        return tracker

    def test_parser_accepts_braces_inside_string_values(self):
        tracker = self._read({
            '#event_name': 'brace_test',
            'properties': {'text': 'value with } and { braces'},
        })
        self.assertEqual(len(tracker.events), 1)
        self.assertEqual(
            tracker.events[0]['properties']['text'],
            'value with } and { braces',
        )

    def test_cursor_does_not_drop_events_from_same_second(self):
        tracker = EventTracker()
        tracker._events = [
            {'cursor': 3, 'ts': '12:00:01', 'event_name': 'third'},
            {'cursor': 2, 'ts': '12:00:00', 'event_name': 'second'},
            {'cursor': 1, 'ts': '12:00:00', 'event_name': 'first'},
        ]
        tracker._next_cursor = 4
        self.assertEqual(
            [event['event_name'] for event in tracker.events_after(1)],
            ['third', 'second'],
        )

    def test_clear_returns_cursor_without_reusing_it(self):
        tracker = EventTracker()
        tracker._events = [{'cursor': 1}]
        tracker._next_cursor = 2
        self.assertEqual(tracker.clear(), 1)
        self.assertEqual(tracker.events, [])
        self.assertEqual(tracker.latest_cursor, 1)

    def test_type_check_uses_wrapped_property_value(self):
        event = _parse_event(json.dumps({
            '#event_name': 'se_test2',
            'properties': {'seq_id': 3},
        }))
        spec = {
            'events': {'se_test2': 'session'},
            'fields': {'seq_id': {'type': 'INT', 'desc': '事件序号'}},
            'event_fields': {
                'se_test2': {'seq_id': {'type': 'INT', 'desc': '事件序号'}},
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            self.assertEqual(analyze_events([event]), [])

    def test_type_check_supports_sheet_type_names(self):
        event = _parse_event(json.dumps({
            '#event_name': 'se_test2',
            'properties': {
                'seq_id': 3,
                'ratio': 1.5,
                'tags': ['a'],
                'did': 'device',
            },
        }))
        spec = {
            'events': {'se_test2': 'session'},
            'fields': {
                'seq_id': {'type': 'LONG'},
                'ratio': {'type': 'FLOAT'},
                'tags': {'type': 'ARRAY_STRING'},
                'did': {'type': 'STRING'},
            },
            'event_fields': {
                'se_test2': {
                    'seq_id': {'type': 'LONG'},
                    'ratio': {'type': 'FLOAT'},
                    'tags': {'type': 'ARRAY_STRING'},
                    'did': {'type': 'STRING'},
                },
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            self.assertEqual(analyze_events([event]), [])

    def test_enum_value_inside_spec_passes(self):
        event = _parse_event(json.dumps({
            '#event_name': 'it_get_test2',
            'properties': {'get_src': 1},
        }))
        spec = {
            'events': {'it_get_test2': '道具获得'},
            'fields': {},
            'event_fields': {
                'it_get_test2': {
                    'get_src': {
                        'type': 'INT',
                        'value_desc': '1：过关\n14：通关问卷赠送',
                    },
                },
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            self.assertEqual(analyze_events([event]), [])

    def test_enum_value_outside_spec_is_an_error(self):
        event = _parse_event(json.dumps({
            '#event_name': 'it_get_test2',
            'properties': {'get_src': 0},
        }))
        spec = {
            'events': {'it_get_test2': '道具获得'},
            'fields': {},
            'event_fields': {
                'it_get_test2': {
                    'get_src': {
                        'type': 'INT',
                        'value_desc': '1：过关\n2：二合合成物\n3：章节通关奖励',
                    },
                },
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            issues = analyze_events([event])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['field'], 'get_src')
        self.assertEqual(issues[0]['level'], 'error')
        self.assertIn('取值 0', issues[0]['msg'])
        self.assertIn('1～3', issues[0]['msg'])

    def test_b_group_result_enum_is_checked(self):
        event = _parse_event(json.dumps({
            '#event_name': 'tt_b_05_go_merge_result',
            'properties': {'result': 3},
        }))
        spec = {
            'events': {'tt_b_05_go_merge_result': '玩家点击行为'},
            'fields': {},
            'event_fields': {
                'tt_b_05_go_merge_result': {
                    'result': {
                        'type': 'INT',
                        'value_desc': '1：点击Build按钮UI；\n2：点击其他位置',
                    },
                },
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            issues = analyze_events([event])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['field'], 'result')
        self.assertEqual(issues[0]['level'], 'error')
        self.assertIn('1～2', issues[0]['msg'])

    def test_range_value_desc_is_not_treated_as_enum(self):
        event = _parse_event(json.dumps({
            '#event_name': 'pop_up_test2',
            'properties': {'ht': 9},
        }))
        spec = {
            'events': {'pop_up_test2': '弹窗'},
            'fields': {},
            'event_fields': {
                'pop_up_test2': {
                    'ht': {
                        'type': 'INT',
                        'value_desc': '-1：处于无限体力状态\n0及以上：当前三消体力库存数量',
                    },
                },
            },
        }
        with patch('lib.event_tracker.load_spec', return_value=spec):
            self.assertEqual(analyze_events([event]), [])

    def test_event_name_must_match_exactly_and_is_an_error(self):
        spec = {
            'events': {'lvl_st_test2': '关卡开始'},
            'fields': {},
            'event_fields': {'lvl_st_test2': {}},
        }
        events = [{'event_name': 'prefix_lvl_st_test2', 'properties': {}}]
        with patch('lib.event_tracker.load_spec', return_value=spec):
            issues = analyze_events(events)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'error')
        self.assertIn('不一致', issues[0]['msg'])

    def test_field_from_another_event_is_an_error(self):
        spec = {
            'events': {'event_a': 'A', 'event_b': 'B'},
            'fields': {
                'field_a': {'type': 'INT'},
                'field_b': {'type': 'INT'},
            },
            'event_fields': {
                'event_a': {'field_a': {'type': 'INT'}},
                'event_b': {'field_b': {'type': 'INT'}},
            },
        }
        events = [{
            'event_name': 'event_a',
            'properties': {'field_a': 1, 'field_b': 2},
        }]
        with patch('lib.event_tracker.load_spec', return_value=spec):
            issues = analyze_events(events)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'error')
        self.assertIn('不属于事件', issues[0]['msg'])

    def test_missing_event_field_is_a_warning(self):
        spec = {
            'events': {'event_a': 'A'},
            'fields': {'required_field': {'type': 'STRING'}},
            'event_fields': {
                'event_a': {'required_field': {'type': 'STRING'}},
            },
        }
        events = [{'event_name': 'event_a', 'properties': {}}]
        with patch('lib.event_tracker.load_spec', return_value=spec):
            issues = analyze_events(events)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'warn')
        self.assertIn('未上报', issues[0]['msg'])

    def test_cp_event_and_cp_fields_are_ignored(self):
        spec = {
            'events': {'cp': '公共参数', 'event_a': 'A'},
            'fields': {
                'did': {'type': 'STRING'},
                'field_a': {'type': 'INT'},
            },
            'event_fields': {
                'cp': {'did': {'type': 'STRING'}},
                'event_a': {'field_a': {'type': 'INT'}},
            },
        }
        events = [
            {'event_name': 'cp', 'properties': {'anything': object()}},
            {'event_name': 'event_a', 'properties': {'field_a': 1, 'did': 99}},
        ]
        with patch('lib.event_tracker.load_spec', return_value=spec):
            self.assertEqual(analyze_events(events), [])


class RelationAnalysisTests(unittest.TestCase):
    """跨事件关联校验：pair / window / sequence 三种规则。"""

    ENG = 'lvl_end_eng_get_vs_it_get_energy'
    COIN = 'lvl_end_coin_get_vs_coin_get'
    LVL = 'lvl_st_vs_lvl_end_fields'

    def _ev(self, cursor, ts, name, **props):
        return {'cursor': cursor, 'ts': ts, 'event_name': name, 'properties': props}

    def _lvl_end(self, cursor, ts, eng_get=10, lvl_fs=1, **extra):
        props = {'eng_get': eng_get, 'lvl_fs': lvl_fs, 'lvl_ne': '400100001'}
        props.update(extra)
        return self._ev(cursor, ts, 'lvl_end', **props)

    def _it_get(self, cursor, ts, get_cnt, res_id='Energy', get_src=1):
        return self._ev(cursor, ts, 'it_get',
                        res_id=res_id, get_src=get_src, get_cnt=get_cnt)

    def _filler(self, cursor, ts):
        return self._ev(cursor, ts, 'other')

    def _issues(self, events, rule_id=None):
        issues = analyze_relations(events)
        if rule_id:
            issues = [i for i in issues if i['relation_id'] == rule_id]
        return issues

    # ---------- pair：过关体力 ----------

    def test_equal_values_pass(self):
        events = [self._lvl_end(1, '10:00:00', 3), self._it_get(2, '10:00:01', 3)]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_mismatch_is_an_error(self):
        events = [self._lvl_end(1, '10:00:00', 3), self._it_get(2, '10:00:01', 5)]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'error')
        self.assertEqual(issues[0]['field'], 'eng_get')
        self.assertIn('eng_get=3', issues[0]['msg'])
        self.assertIn('get_cnt=5', issues[0]['msg'])

    def test_failed_level_is_not_checked(self):
        events = [self._lvl_end(1, '10:00:00', 0, lvl_fs=2),
                  self._it_get(2, '10:00:01', 5), self._filler(3, '10:01:00')]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_zero_reward_still_pairs_and_catches_mismatch(self):
        """eng_get 取成 0 时不能静默跳过，否则这类取值 bug 查不出来。"""
        events = [self._lvl_end(1, '10:00:00', 0), self._it_get(2, '10:00:01', 5)]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'error')
        self.assertIn('eng_get=0', issues[0]['msg'])
        self.assertIn('get_cnt=5', issues[0]['msg'])

    def test_zero_reward_without_it_get_warns(self):
        events = [self._lvl_end(1, '10:00:00', 0), self._filler(2, '10:10:00')]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'warn')
        self.assertIn('未等到', issues[0]['msg'])

    def test_missing_eng_get_is_reported(self):
        events = [self._ev(1, '10:00:00', 'lvl_end', lvl_fs=1, lvl_ne='400100001'),
                  self._filler(2, '10:00:01')]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'warn')
        self.assertIn('eng_get', issues[0]['msg'])
        self.assertIn('无法核对', issues[0]['msg'])

    def test_non_numeric_eng_get_is_reported(self):
        events = [self._lvl_end(1, '10:00:00', ''), self._filler(2, '10:00:01')]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertIn('不是有效值', issues[0]['msg'])

    def test_other_get_src_does_not_pair(self):
        events = [self._lvl_end(1, '10:00:00', 5),
                  self._it_get(2, '10:00:01', 5, get_src=3),
                  self._filler(3, '10:10:00')]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertIn('未等到', issues[0]['msg'])

    def test_other_resource_does_not_pair(self):
        events = [self._lvl_end(1, '10:00:00', 5),
                  self._it_get(2, '10:00:01', 5, res_id='Coin'),
                  self._filler(3, '10:10:00')]
        self.assertEqual(len(self._issues(events, self.ENG)), 1)

    def test_b_group_guide_start_end_pair(self):
        events = [self._ev(1, '10:00:00', 'tt_b_04_go_merge_start'),
                  self._ev(2, '10:00:05', 'tt_b_06_go_merge_end')]
        self.assertEqual(self._issues(events, 'tt_b_go_merge_pair'), [])

    def test_b_group_guide_missing_end_warns(self):
        events = [self._ev(1, '10:00:00', 'tt_b_04_go_merge_start'),
                  self._filler(2, '10:06:00')]
        issues = self._issues(events, 'tt_b_go_merge_pair')
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['level'], 'warn')
        self.assertIn('tt_b_06_go_merge_end', issues[0]['msg'])

    def test_b_group_guide_end_without_start_is_ignored(self):
        events = [self._ev(1, '10:00:00', 'tt_b_06_go_merge_end')]
        self.assertEqual(self._issues(events, 'tt_b_go_merge_pair'), [])

    def test_all_b_group_pairs_accept_matching_start_and_end(self):
        rules = [r for r in RELATION_RULES if r['id'].startswith('tt_b_')]
        self.assertEqual(len(rules), 8)
        for rule in rules:
            events = [
                self._ev(1, '10:00:00', rule['trigger']['event']),
                self._ev(2, '10:00:05', rule['expect']['event']),
            ]
            self.assertEqual(self._issues(events, rule['id']), [], rule['id'])

    def test_multiple_levels_pair_in_order(self):
        events = [self._lvl_end(1, '10:00:00', 3), self._it_get(2, '10:00:01', 3),
                  self._lvl_end(3, '10:00:10', 7), self._it_get(4, '10:00:11', 7)]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_crossed_pairing_is_detected(self):
        events = [self._lvl_end(1, '10:00:00', 3), self._lvl_end(2, '10:00:01', 7),
                  self._it_get(3, '10:00:02', 7), self._it_get(4, '10:00:03', 3)]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(i['level'] == 'error' for i in issues))

    def test_unsorted_input_is_ordered_by_cursor(self):
        events = [self._it_get(2, '10:00:01', 4), self._lvl_end(1, '10:00:00', 4)]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_wrapped_property_values_are_unwrapped(self):
        events = [
            {'cursor': 1, 'ts': '10:00:00', 'event_name': 'lvl_end',
             'properties': {'eng_get': {'value': 6, 'desc': '获得体力'}, 'lvl_fs': 1}},
            {'cursor': 2, 'ts': '10:00:01', 'event_name': 'it_get',
             'properties': {'res_id': {'value': 'Energy', 'desc': '资源'},
                            'get_src': 1, 'get_cnt': {'value': 6, 'desc': '数量'}}},
        ]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_event_time_is_used_for_window(self):
        events = [
            {'cursor': 1, 'ts': '10:00:00', 'event_time': 1000000,
             'event_name': 'lvl_end', 'properties': {'eng_get': 3, 'lvl_fs': 1}},
            {'cursor': 2, 'ts': '10:00:01', 'event_time': 1005000,
             'event_name': 'it_get',
             'properties': {'res_id': 'Energy', 'get_src': 1, 'get_cnt': 3}},
        ]
        self.assertEqual(self._issues(events, self.ENG), [])

    def test_next_level_start_closes_the_window(self):
        """下一关都开始了还没等到体力入账 —— 算漏报，而不是一直挂着。"""
        events = [self._lvl_end(1, '10:00:00', 5),
                  self._ev(2, '10:00:30', 'lvl_st', lvl_ne='400100002')]
        issues = self._issues(events, self.ENG)
        self.assertEqual(len(issues), 1)
        self.assertIn('未等到', issues[0]['msg'])

    # ---------- pair：金币 / 进出关卡口径 ----------

    def test_coin_pair_matches(self):
        events = [self._lvl_end(1, '10:00:00', 10, coin_get=20),
                  self._ev(2, '10:00:01', 'coin_get', get_src=1, get_cnt=20)]
        self.assertEqual(self._issues(events, self.COIN), [])

    def test_coin_pair_mismatch(self):
        events = [self._lvl_end(1, '10:00:00', 10, coin_get=20),
                  self._ev(2, '10:00:01', 'coin_get', get_src=1, get_cnt=60)]
        issues = self._issues(events, self.COIN)
        self.assertEqual(len(issues), 1)
        self.assertIn('coin_get=20', issues[0]['msg'])

    def test_level_identity_fields_must_match(self):
        base = dict(lvl_ne='400100001', lvl_ty=1, st_cnt_ne=1,
                    st_cnt_id=1, pp_use=0)
        events = [self._ev(1, '10:00:00', 'lvl_st', **base),
                  self._ev(2, '10:01:00', 'lvl_end', **{**base, 'lvl_fs': 1})]
        self.assertEqual(self._issues(events, self.LVL), [])

        events[1]['properties']['st_cnt_id'] = 3
        issues = self._issues(events, self.LVL)
        self.assertEqual(len(issues), 1)
        self.assertIn('st_cnt_id', issues[0]['msg'])

    # ---------- pair：订单 / 反向 ----------

    def test_order_ids_must_match(self):
        events = [self._ev(1, '10:00:00', 'ord_do', ord_id='O-1'),
                  self._ev(2, '10:00:05', 'ord_cmt', ord_id='O-1')]
        self.assertEqual(self._issues(events, 'ord_do_vs_ord_cmt'), [])

        events[1]['properties']['ord_id'] = 'O-2'
        self.assertEqual(len(self._issues(events, 'ord_do_vs_ord_cmt')), 1)

    def test_missing_order_submit_is_not_an_error(self):
        events = [self._ev(1, '10:00:00', 'ord_do', ord_id='O-1')]
        self.assertEqual(self._issues(events, 'ord_do_vs_ord_cmt'), [])

    def test_reverse_rule_matches_sp_gen_with_prior_it_use(self):
        events = [self._ev(1, '10:00:00', 'it_use', res_id='Energy', use_src=6, use_cnt=2),
                  self._ev(2, '10:00:03', 'sp_gen_ptr', eng_use=2)]
        self.assertEqual(self._issues(events, 'sp_gen_energy_use'), [])

        events[1]['properties']['eng_use'] = 3
        self.assertEqual(len(self._issues(events, 'sp_gen_energy_use')), 1)

    # ---------- window ----------

    def test_window_sum_matches(self):
        events = [
            self._ev(1, '10:00:00', 'lvl_st', lvl_ne='L1'),
            self._ev(2, '10:00:10', 'coin_use', use_src=1, use_cnt=100),
            self._ev(3, '10:00:20', 'coin_use', use_src=3, use_cnt=50),
            self._ev(4, '10:01:00', 'lvl_end', lvl_fs=1, coin_use=150),
        ]
        self.assertEqual(self._issues(events, 'lvl_end_coin_use_sum'), [])

        events[3]['properties']['coin_use'] = 160
        issues = self._issues(events, 'lvl_end_coin_use_sum')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 160', issues[0]['msg'])
        self.assertIn('明细累加 150', issues[0]['msg'])

    def test_window_count_matches(self):
        events = [
            self._ev(1, '10:00:00', 'lvl_st'),
            self._ev(2, '10:00:10', 'coin_use', use_src=1, use_cnt=100),
            self._ev(3, '10:00:40', 'coin_use', use_src=1, use_cnt=100),
            self._ev(4, '10:01:00', 'lvl_end', lvl_fs=1, ego_cnt=2),
        ]
        self.assertEqual(self._issues(events, 'lvl_end_ego_cnt'), [])

    def test_chapter_counter_delta_matches(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st', tp_cnt=10, mg_cnt=20),
            self._ev(2, '10:03:00', 'chp_end', tp_cnt=13, mg_cnt=27,
                     chp_tp_cnt=3, chp_mg_cnt=7),
        ]
        self.assertEqual(self._issues(events, 'chp_end_tp_cnt_delta'), [])
        self.assertEqual(self._issues(events, 'chp_end_mg_cnt_delta'), [])

    def test_chapter_counter_delta_mismatch(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st', tp_cnt=10, mg_cnt=20),
            self._ev(2, '10:03:00', 'chp_end', tp_cnt=13, mg_cnt=27,
                     chp_tp_cnt=4, chp_mg_cnt=7),
        ]
        issues = self._issues(events, 'chp_end_tp_cnt_delta')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 4', issues[0]['msg'])
        self.assertIn('按会话累计值算 3', issues[0]['msg'])

    def test_chapter_counter_delta_skipped_without_start_counters(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st'),
            self._ev(2, '10:03:00', 'chp_end', tp_cnt=13, mg_cnt=27,
                     chp_tp_cnt=99, chp_mg_cnt=99),
        ]
        self.assertEqual(self._issues(events, 'chp_end_tp_cnt_delta'), [])
        self.assertEqual(self._issues(events, 'chp_end_mg_cnt_delta'), [])

    def test_chapter_remaining_energy_sum_matches(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st'),
            self._it_get(2, '10:02:00', 18, get_src=6),
            self._it_get(3, '10:02:30', 20, get_src=3),
            self._ev(4, '10:03:00', 'chp_end', chp_rem_eng=18),
        ]
        self.assertEqual(self._issues(events, 'chp_end_rem_eng_sum'), [])

        events[3]['properties']['chp_rem_eng'] = 19
        issues = self._issues(events, 'chp_end_rem_eng_sum')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 19', issues[0]['msg'])
        self.assertIn('明细累加 18', issues[0]['msg'])

    def test_chapter_generator_use_count(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st'),
            self._ev(2, '10:00:10', 'gen_tap', gen_type=1, gen_num=3),
            self._ev(3, '10:00:20', 'gen_tap', gen_type=1, gen_num=1),
            self._ev(4, '10:00:30', 'gen_tap', gen_type=1, gen_num=5),
            self._ev(5, '10:03:00', 'chp_end', chp_gen_use=1,
                     chp_sp_gen_use=0),
        ]
        self.assertEqual(self._issues(events, 'chp_end_gen_use_count'), [])

        events[4]['properties']['chp_gen_use'] = 2
        issues = self._issues(events, 'chp_end_gen_use_count')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 2', issues[0]['msg'])
        self.assertIn('明细累加 1', issues[0]['msg'])

    def test_chapter_generator_use_count_includes_special_generators(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st'),
            self._ev(2, '10:00:10', 'gen_tap', gen_type=1, gen_num=1),
            self._ev(3, '10:00:20', 'gen_tap', gen_type=2),
            self._ev(4, '10:00:30', 'sp_gen_scr', gen_idx=11, eng_use=1),
            self._ev(5, '10:00:40', 'sp_gen_scr', gen_idx=12, eng_use=1),
            self._ev(6, '10:03:00', 'chp_end', chp_gen_use=3,
                     chp_sp_gen_use=2),
        ]
        self.assertEqual(self._issues(events, 'chp_end_gen_use_count'), [])

        events[5]['properties']['chp_gen_use'] = 2
        issues = self._issues(events, 'chp_end_gen_use_count')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 2', issues[0]['msg'])
        self.assertIn('明细累加 3', issues[0]['msg'])
        self.assertIn('普通生成器用完 1', issues[0]['msg'])
        self.assertIn('特殊生成器用完 2', issues[0]['msg'])

    def test_chapter_special_generator_consumed_count(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st'),
            # 用过但还有 11 次剩余，后面倒计时结束 —— 不算用完
            self._ev(2, '10:00:10', 'sp_gen_fruit', gen_idx=1, is_t_end=0,
                     lft_st=11, eng_use=1),
            self._ev(3, '10:00:20', 'sp_gen_fruit', gen_idx=1, is_t_end=1),
            # 单次用完的两个刮刮卡
            self._ev(4, '10:00:30', 'sp_gen_scr', gen_idx=11, eng_use=1),
            self._ev(5, '10:00:40', 'sp_gen_scr', gen_idx=12, eng_use=1),
            self._ev(6, '10:03:00', 'chp_end', chp_sp_gen_use=2),
        ]
        self.assertEqual(self._issues(events, 'chp_end_sp_gen_use_count'), [])

        events[5]['properties']['chp_sp_gen_use'] = 3
        issues = self._issues(events, 'chp_end_sp_gen_use_count')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 3', issues[0]['msg'])
        self.assertIn('明细累加 2', issues[0]['msg'])

    def test_chapter_start_energy_matches(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st', chp_id='Chapter_1', eng=12),
            self._ev(2, '10:03:00', 'chp_end', eng=118, chp_st_eng=0),
            self._ev(3, '10:03:05', 'chp_st', chp_id='Chapter_2', eng=118),
            self._ev(4, '10:06:00', 'chp_end', eng=180, chp_st_eng=118),
        ]
        self.assertEqual(self._issues(events, 'chp_end_st_eng_vs_start_eng'), [])

        events[3]['properties']['chp_st_eng'] = 119
        issues = self._issues(events, 'chp_end_st_eng_vs_start_eng')
        self.assertEqual(len(issues), 1)
        self.assertIn('上报 119', issues[0]['msg'])
        self.assertIn('上一章结束时 eng=118', issues[0]['msg'])

    def test_first_chapter_carried_energy_must_be_zero(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st', chp_id='Chapter_1', eng=12),
            self._ev(2, '10:03:00', 'chp_end', eng=118, chp_st_eng=1),
        ]
        issues = self._issues(events, 'chp_end_st_eng_vs_start_eng')
        self.assertEqual(len(issues), 1)
        self.assertIn('第一章带入 eng=0', issues[0]['msg'])

    def test_carried_energy_skipped_when_log_starts_mid_chapter(self):
        events = [
            self._ev(1, '10:00:00', 'chp_st', chp_id='Chapter_3', eng=180),
            self._ev(2, '10:03:00', 'chp_end', eng=251, chp_st_eng=180),
        ]
        self.assertEqual(self._issues(events, 'chp_end_st_eng_vs_start_eng'), [])

    def test_window_is_skipped_when_capture_has_a_gap(self):
        """窗口里日志断了 20 分钟 —— 不能拿残缺数据判错。"""
        events = [
            self._ev(1, '10:00:00', 'lvl_st'),
            self._ev(2, '10:20:00', 'lvl_end', lvl_fs=1, coin_use=900),
        ]
        self.assertEqual(self._issues(events, 'lvl_end_coin_use_sum'), [])

    def test_previous_event_window(self):
        events = [
            self._ev(1, '10:00:00', 'ord_cmt', ord_id='O1', eng_use=0, lvl_cnt=0),
            self._ev(2, '10:00:10', 'it_use', res_id='Energy', use_src=5, use_cnt=8),
            self._ev(3, '10:00:20', 'lvl_end', lvl_fs=1, eng_get=10),
            self._ev(4, '10:00:30', 'ord_cmt', ord_id='O2', eng_use=8, lvl_cnt=1),
        ]
        self.assertEqual(self._issues(events, 'ord_cmt_counters'), [])

        events[3]['properties']['eng_use'] = 9
        issues = self._issues(events, 'ord_cmt_counters')
        self.assertEqual(len(issues), 1)
        self.assertIn('eng_use', issues[0]['msg'])

    def test_cumulative_window(self):
        events = [
            self._ev(1, '10:00:00', 'evt_pc_st', evt_id='E1'),
            self._ev(2, '10:00:10', 'lvl_end', lvl_fs=1, eng_get=10),
            self._ev(3, '10:00:20', 'lvl_end', lvl_fs=1, eng_get=10),
            self._ev(4, '10:00:30', 'evt_pc_rd', evt_id='E1', evt_lvl_cnt=2),
            self._ev(5, '10:00:40', 'lvl_end', lvl_fs=1, eng_get=10),
            self._ev(6, '10:00:50', 'evt_pc_rd', evt_id='E1', evt_lvl_cnt=3),
        ]
        self.assertEqual(self._issues(events, 'evt_pc_lvl_cnt'), [])

        events[5]['properties']['evt_lvl_cnt'] = 4
        self.assertEqual(len(self._issues(events, 'evt_pc_lvl_cnt')), 1)

    # ---------- sequence ----------

    def test_coin_balance_chain(self):
        events = [
            self._ev(1, '10:00:00', 'coin_get', get_cnt=20, fn_cnt=120),
            self._ev(2, '10:00:10', 'coin_use', use_cnt=30, fn_cnt=90),
            self._ev(3, '10:00:20', 'coin_get', get_cnt=5, fn_cnt=95),
        ]
        self.assertEqual(self._issues(events, 'coin_balance_chain'), [])

        events[2]['properties']['fn_cnt'] = 130
        issues = self._issues(events, 'coin_balance_chain')
        self.assertEqual(len(issues), 1)
        self.assertIn('上次 fn_cnt=90', issues[0]['msg'])

    def test_resource_chain_is_grouped_by_res_id(self):
        events = [
            self._ev(1, '10:00:00', 'it_get', res_id='Energy', get_cnt=10, fn_cnt=100),
            self._ev(2, '10:00:05', 'it_get', res_id='Booster_Hammer', get_cnt=1, fn_cnt=4),
            self._ev(3, '10:00:10', 'it_use', res_id='Energy', use_cnt=5, fn_cnt=95),
        ]
        self.assertEqual(self._issues(events, 'resource_balance_chain'), [])

    def test_lives_is_excluded_from_chain(self):
        events = [
            self._ev(1, '10:00:00', 'it_get', res_id='Lives', get_cnt=1, fn_cnt=5),
            self._ev(2, '10:00:10', 'it_use', res_id='Lives', use_cnt=1, fn_cnt=2),
        ]
        self.assertEqual(self._issues(events, 'resource_balance_chain'), [])

    def test_chain_resets_on_account_switch(self):
        events = [
            {'cursor': 1, 'ts': '10:00:00', 'account_id': 'A', 'event_name': 'coin_get',
             'properties': {'get_cnt': 20, 'fn_cnt': 120}},
            {'cursor': 2, 'ts': '10:00:05', 'account_id': 'B', 'event_name': 'coin_get',
             'properties': {'get_cnt': 5, 'fn_cnt': 5}},
        ]
        self.assertEqual(self._issues(events, 'coin_balance_chain'), [])

    def test_chain_resets_after_long_gap(self):
        events = [
            self._ev(1, '10:00:00', 'coin_get', get_cnt=20, fn_cnt=120),
            self._ev(2, '11:00:00', 'coin_get', get_cnt=5, fn_cnt=5),
        ]
        self.assertEqual(self._issues(events, 'coin_balance_chain'), [])

    def test_empty_input(self):
        self.assertEqual(analyze_relations([]), [])


class RulesDocTests(unittest.TestCase):
    """界面「校验规则」面板的数据必须始终可读，避免以后加规则忘了写说明。"""

    def test_every_rule_has_title_and_plain_text(self):
        data = describe_rules()
        self.assertTrue(data['sections'])
        for section in data['sections']:
            self.assertTrue(section['title'], section)
            self.assertTrue(section['items'], section)
            for item in section['items']:
                self.assertTrue(item['title'], item)
                self.assertTrue(item['plain'].strip(), item)
                self.assertGreater(len(item['plain']), 20, item)

    def test_relation_rules_ship_a_handwritten_plain_text(self):
        for rule in RELATION_RULES:
            self.assertTrue(rule.get('plain', '').strip(), rule['id'])

    def test_panel_lists_only_the_three_live_sections(self):
        data = describe_rules()
        self.assertEqual(
            [section['key'] for section in data['sections']],
            ['single_event', 'relation', 'spec_self_check'],
        )
        self.assertNotIn('not_covered', data)

    def test_spec_stats_come_from_the_live_spec(self):
        data = describe_rules()
        self.assertGreater(data['spec_field_count'], 0)
        self.assertGreaterEqual(data['value_desc_count'], 0)

    def test_rule_ids_are_unique_and_counted(self):
        ids = [rule['id'] for rule in RELATION_RULES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(describe_rules()['rule_count'], len(ids))

    def test_stats_report_every_rule_that_runs(self):
        events = [
            {'cursor': 1, 'ts': '10:00:00', 'event_name': 'lvl_end',
             'properties': {'eng_get': 3, 'lvl_fs': 1}},
            {'cursor': 2, 'ts': '10:00:01', 'event_name': 'it_get',
             'properties': {'res_id': 'Energy', 'get_src': 1, 'get_cnt': 3}},
        ]
        stats = {}
        analyze_relations(events, stats=stats)
        self.assertEqual(stats['lvl_end_eng_get_vs_it_get_energy']['checked'], 1)
        self.assertEqual(stats['lvl_end_eng_get_vs_it_get_energy']['issues'], 0)


if __name__ == '__main__':
    unittest.main()
