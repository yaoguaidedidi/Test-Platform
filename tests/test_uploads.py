import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import server


class UploadTests(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(server.HuTestHandler)
        response = {}

        def fail(message, status=500):
            raise AssertionError(f'{status}: {message}')

        handler._json = response.update
        handler._error = fail
        return handler, response

    def test_multipart_parses_chinese_filename(self):
        handler, response = self.make_handler()
        fn = '关卡动画跳帧20260812-150810.mp4'
        raw = (
            b'--BOUND\r\n'
            b'Content-Disposition: form-data; name="files"; filename="'
            + fn.encode('utf-8') + b'"\r\n'
            b'Content-Type: video/mp4\r\n\r\n'
            b'data\r\n'
            b'--BOUND--\r\n'
        )
        ct = 'multipart/form-data; boundary=BOUND'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, 'DATA_DIR', tmp):
            result = handler._parse_multipart(raw, ct)
            files = result['files']
            self.assertEqual(files['filename'], fn)
            self.assertTrue(os.path.isfile(files['filepath']))

    def test_clear_uploads_all_keeps_only_referenced(self):
        handler, response = self.make_handler()
        with tempfile.TemporaryDirectory() as tmp:
            uploads = os.path.join(tmp, 'uploads')
            queue = os.path.join(tmp, 'bug_queue')
            os.makedirs(uploads)
            os.makedirs(queue)
            old_ts = time.time() - 10 * 86400

            with patch.object(server, 'DATA_DIR', tmp):
                old_unref = os.path.join(uploads, 'old_unref.mp4')
                with open(old_unref, 'wb') as f:
                    f.write(b'x')
                os.utime(old_unref, (old_ts, old_ts))

                old_ref = os.path.join(uploads, 'old_ref.mp4')
                with open(old_ref, 'wb') as f:
                    f.write(b'x')
                os.utime(old_ref, (old_ts, old_ts))

                new_file = os.path.join(uploads, 'new.mp4')
                with open(new_file, 'wb') as f:
                    f.write(b'x')

                bug = {'id': 'b1', 'bug': {'attachments': [old_ref]}}
                with open(os.path.join(queue, 'b1.json'), 'w',
                          encoding='utf-8') as f:
                    json.dump(bug, f)

                handler._handle_clear_uploads({'days': 'all'})

            self.assertFalse(os.path.exists(old_unref))
            self.assertTrue(os.path.exists(old_ref))
            self.assertFalse(os.path.exists(new_file))
            self.assertEqual(response['deleted'], 2)


if __name__ == '__main__':
    unittest.main()
