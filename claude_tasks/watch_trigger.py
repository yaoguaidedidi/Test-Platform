#!/usr/bin/env python3
"""Watch trigger file and print when a new draft needs processing."""
import os, time, json, sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from lib.bug_queue import QUEUE_DIR

TRIGGER = os.path.join(BASE_DIR, 'data', '.trigger')
last_id = None

while True:
    if os.path.exists(TRIGGER):
        with open(TRIGGER, 'r') as f:
            bug_id = f.read().strip()
        if bug_id and bug_id != last_id:
            # Check it's still a draft
            path = os.path.join(QUEUE_DIR, f'{bug_id}.json')
            if os.path.exists(path):
                with open(path) as f:
                    bug = json.load(f)
                if bug['status'] == 'draft':
                    print(f'SIGNAL:{bug_id}')
                    last_id = bug_id
    time.sleep(2)
