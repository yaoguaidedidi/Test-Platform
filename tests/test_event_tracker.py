import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lib.event_tracker import EventTracker, _parse_event, analyze_events


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


if __name__ == '__main__':
    unittest.main()
