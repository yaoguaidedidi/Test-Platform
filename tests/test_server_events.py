import unittest
from unittest.mock import patch

import server


class FakeTracker:
    def __init__(self):
        self.events = [{'cursor': 1, 'event_name': 'old_bad_event'}]
        self.latest_cursor = 1
        self.is_running = True

    def events_after(self, _cursor):
        return []


class EventApiTests(unittest.TestCase):
    def test_incremental_response_analyzes_all_retained_events(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}
        handler._json = response.update
        tracker = FakeTracker()
        issues = [{'event': 'old_bad_event', 'level': 'warn', 'msg': 'bad'}]

        with (
            patch.object(server, '_event_tracker', tracker),
            patch('lib.event_tracker.analyze_events', return_value=issues) as analyze,
        ):
            handler._handle_events({'after': ['1']})

        analyze.assert_called_once_with(tracker.events)
        self.assertEqual(response['events'], [])
        self.assertEqual(response['issues'], issues)
        self.assertEqual(response['total'], 1)


if __name__ == '__main__':
    unittest.main()
