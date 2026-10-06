"""Private process ownership; duplicate startup cannot write health or poll."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.ownership import AlreadyRunning, ProcessLease


class OwnershipTests(unittest.TestCase):
    def test_lock_excludes_second_writer_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as root:
            first = ProcessLease(root).acquire()
            try:
                with self.assertRaises(AlreadyRunning):
                    ProcessLease(root).acquire()
                self.assertEqual((Path(root) / 'bridge.lock').stat().st_mode & 0o777, 0o600)
            finally:
                first.close()
            second = ProcessLease(root).acquire()
            second.close()

    def test_duplicate_main_does_not_poll_announce_or_write_snapshot(self):
        with mock.patch.object(tgbridge, 'startup_smoke'), \
             mock.patch.object(tgbridge, 'ensure_private_storage'), \
             mock.patch.object(tgbridge, 'load_json', return_value={'bot_token': 'fixture'}), \
             mock.patch.object(tgbridge.ProcessLease, 'acquire', side_effect=AlreadyRunning('owned')), \
             mock.patch.object(tgbridge, 'run') as run, \
             mock.patch.object(tgbridge, 'update_health') as health, \
             mock.patch.object(tgbridge, 'announce_all') as announce:
            with self.assertRaises(SystemExit):
                tgbridge.main()
            run.assert_not_called(); health.assert_not_called(); announce.assert_not_called()


if __name__ == '__main__':
    unittest.main()
