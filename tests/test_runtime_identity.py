"""Version evidence must work with archives and remain frozen until restart."""
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest import mock

from tgbridge_core.runtime import source_identity
import tgbridge


class RuntimeIdentityTests(unittest.TestCase):
    def test_archive_digest_covers_entrypoint_and_core_not_runtime_state(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'tgbridge_core').mkdir()
            entry = root / 'tgbridge.py'
            entry.write_text('entry')
            module = root / 'tgbridge_core' / 'module.py'
            module.write_text('module')
            with mock.patch('tgbridge_core.runtime.subprocess.run', side_effect=FileNotFoundError):
                before = source_identity(root)
                self.assertIsNone(before['revision'])
                self.assertEqual(len(before['source_sha256']), 64)
                (root / 'state.json').write_text('private')
                self.assertEqual(source_identity(root), before)
                module.write_text('updated module')
                self.assertNotEqual(source_identity(root), before)
                module.write_text('module')
                entry.write_text('updated entry')
                self.assertNotEqual(source_identity(root), before)

    def test_revision_and_digest_are_separate_and_git_failure_is_optional(self):
        root = Path(tgbridge.__file__).parent
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('git', 2)):
            with mock.patch('tgbridge_core.runtime.subprocess.run', side_effect=error):
                self.assertIsNone(source_identity(root)['revision'])
                self.assertIsNotNone(source_identity(root)['source_sha256'])

    def test_loaded_snapshot_does_not_follow_later_disk_updates(self):
        before = dict(tgbridge.LOADED_CODE)
        with mock.patch('tgbridge_core.runtime.source_identity', return_value={'revision': 'new'}):
            self.assertEqual(tgbridge.LOADED_CODE, before)


if __name__ == '__main__':
    unittest.main()
