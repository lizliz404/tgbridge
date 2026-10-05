#!/usr/bin/env python3
"""Isolated review runner; network is prohibited even if a mock is missed."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]
with tempfile.TemporaryDirectory(prefix='tgbridge-test-') as tmp:
    home = Path(tmp)
    for d in ('config', 'sessions', 'agent', 'tmp', 'bin'):
        (home / d).mkdir()
    fake = home / 'bin' / 'runner'
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o700)
    os.environ.update(HOME=tmp, XDG_CONFIG_HOME=str(home/'config'),
                      PI_CODING_AGENT_DIR=str(home/'agent'),
                      PI_CODING_AGENT_SESSION_DIR=str(home/'sessions'),
                      TMPDIR=str(home/'tmp'), PI_OFFLINE='1',
                      PI_BIN=str(fake), OPENCODE_BIN=str(fake), CODEX_BIN=str(fake))
    tempfile.tempdir = str(home/'tmp')
    import tgbridge
    import tgbridge_core.runners as runners
    runners.OPENCODE = str(fake)
    runners.PI = str(fake)
    with mock.patch('socket.socket.connect', side_effect=AssertionError('OFFLINE: network forbidden')), \
         mock.patch('urllib.request.urlopen', side_effect=AssertionError('OFFLINE: HTTP forbidden')):
        suite = unittest.defaultTestLoader.discover(str(REPO/'tests'))
        if '--regressions' in sys.argv:
            suite = unittest.defaultTestLoader.loadTestsFromName('test_pi_rpc_regressions')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(not result.wasSuccessful())
