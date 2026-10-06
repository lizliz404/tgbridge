"""Real SIGTERM in a private child; no production service or bot is touched."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == 'posix', 'POSIX signal acceptance')
class SignalShutdownTests(unittest.TestCase):
    def test_sigterm_during_network_call_reaches_main_stop_handler(self):
        script = '''
import signal
from unittest import mock
import tgbridge

def blocked_network(*args, **kwargs):
    print('READY', flush=True)
    signal.pause()

with mock.patch.object(tgbridge, 'startup_smoke'), \\
     mock.patch.object(tgbridge, 'ensure_private_storage'), \\
     mock.patch.object(tgbridge, 'load_json', return_value={'bot_token':'fixture'}), \\
     mock.patch.object(tgbridge, 'update_health', side_effect=lambda **k: print(k['status'], flush=True)), \\
     mock.patch.object(tgbridge, 'audit'), \\
     mock.patch.object(tgbridge, 'announce_all'), \\
     mock.patch.object(tgbridge, 'systemd_notify'), \\
     mock.patch.object(tgbridge.urllib.request, 'urlopen', side_effect=blocked_network), \\
     mock.patch.object(tgbridge, 'run', side_effect=lambda cfg: tgbridge.api('fixture', 'getUpdates')):
    tgbridge.main()
'''
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen([sys.executable, '-u', '-c', script], cwd=root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            # Bounded readiness: do not send SIGTERM before handler installation.
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), 'fixture never reached network call')
            self.assertEqual(proc.stdout.readline().strip(), 'READY')
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn('stopped', out)
            self.assertNotIn('crashed', out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
