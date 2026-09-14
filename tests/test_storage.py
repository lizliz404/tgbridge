import os
import stat
import tempfile
import unittest

from tgbridge_core.storage import ensure_private_dir, load_json, save_json


class StorageTests(unittest.TestCase):
    def test_private_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "runtime")
            ensure_private_dir(directory)
            path = os.path.join(directory, "state.json")
            save_json(path, {"sessions": {"chat": "session"}})
            self.assertEqual(load_json(path, {}), {"sessions": {"chat": "session"}})
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_invalid_json_uses_default(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "broken.json")
            with open(path, "w") as handle:
                handle.write("{")
            self.assertEqual(load_json(path, {"fallback": True}), {"fallback": True})


if __name__ == "__main__":
    unittest.main()
