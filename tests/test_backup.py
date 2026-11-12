"""Retention safety, interruption, lock, and status contracts without AWS calls."""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

from fixture import ROOT
sys.path.insert(0, str(ROOT / 'scripts'))
from backup import Backups, check_status, identifier  # noqa: E402


def record(number, full, completed, database='app'):
    return {'id': f'{number:032x}', 'full': f'{full:032x}', 'completed': completed, 'database': database}


class Retention(unittest.TestCase):
    def manager(self, records):
        manager = object.__new__(Backups)
        manager.config = {'retention_days': 10}
        manager.prefix = 'test/'
        manager.records = lambda: records
        manager.delete_prefix = Mock()
        return manager

    def test_in_window_child_pins_old_full(self):
        now = 100 * 86400
        manager = self.manager([record(1, 1, 1), record(2, 1, now - 1), record(3, 3, now)])
        self.assertEqual(manager.retain(now), [])
        manager.delete_prefix.assert_not_called()

    def test_last_family_is_kept_even_when_stale_for_each_database(self):
        manager = self.manager([record(1, 1, 1), record(2, 2, 2, 'other')])
        self.assertEqual(manager.retain(100 * 86400), [])

    def test_children_deleted_before_full(self):
        manager = self.manager([record(1, 1, 1), record(2, 1, 2), record(3, 3, 3)])
        self.assertEqual(set(manager.retain(100 * 86400)), {f'{1:032x}', f'{2:032x}'})
        calls = [call.args[0] for call in manager.delete_prefix.call_args_list]
        self.assertEqual(calls, [f'test/data/{2:032x}/', f'test/catalog/{2:032x}.json',
                                 f'test/data/{1:032x}/', f'test/catalog/{1:032x}.json'])

    def test_deletion_failure_cannot_reach_base_or_new_family(self):
        for failed_call in [1, 2]:
            manager = self.manager([record(1, 1, 1), record(2, 1, 2), record(3, 3, 3)])
            manager.delete_prefix.side_effect = [None] * (failed_call - 1) + [RuntimeError('locked object')]
            with self.assertRaises(RuntimeError):
                manager.retain(100 * 86400)
            for call in manager.delete_prefix.call_args_list:
                self.assertNotIn(f'{1:032x}', call.args[0])
                self.assertNotIn(f'{3:032x}', call.args[0])

    def test_status_requires_success_and_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            for state, age, accepted in [('success', 1, True), ('failure', 1, False), ('running', 1, False), ('success', 5000, False)]:
                path.write_text(json.dumps({'state': state, 'completed': time.time() - age}))
                if accepted:
                    check_status(path, 3600)
                else:
                    with self.assertRaises(RuntimeError):
                        check_status(path, 3600)

    def test_concurrent_process_cannot_enter_or_overwrite_active_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            status = path / 'status.json'
            status.write_text('{"state":"running"}')
            config = path / 'config.json'
            config.write_text(json.dumps({'status': str(status)}))
            with open(str(status) + '.lock', 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), str(config), 'run'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(status.read_text()), {'state': 'running'})

    def test_reject_identifier_injection(self):
        for value in ['default; DROP DATABASE x', '../data', "x'", 'a.b.c', '']:
            with self.assertRaises(ValueError):
                identifier(value)

    def test_dangling_catalog_fails_closed(self):
        s3 = Mock()
        value = record(2, 1, 100)
        s3.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': 'test/catalog/' + value['id'] + '.json'}]}]
        s3.get_object.return_value = {'Body': io.BytesIO(json.dumps(value).encode())}
        manager = Backups({'node': 'replica1', 'bucket': 'test', 'prefix': 'test', 'url': 'https://example.invalid/test'}, Mock(), s3)
        with self.assertRaises(KeyError):
            manager.records()
        s3.delete_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
