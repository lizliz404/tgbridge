import urllib.error
import unittest
from unittest import mock

from tgbridge_core.health import (
    classify_network_error,
    proxy_diagnostics,
    polling_health,
    redact_proxy_url,
    systemd_notify,
)


class HealthTests(unittest.TestCase):
    def test_poll_readiness_requires_live_pid_and_recent_success(self):
        health = {"pid": 123, "status": "healthy", "last_poll_ok_at": "2026-10-06T03:00:00+0000"}
        with mock.patch("tgbridge_core.health.os.kill"), \
             mock.patch("tgbridge_core.health.time.time", return_value=1791255650):
            result = polling_health(health)
        self.assertTrue(result["ok"])
        self.assertEqual(result["poll_age_s"], 50)

    def test_dead_pid_and_missing_snapshot_are_not_healthy(self):
        health = {"pid": 123, "status": "healthy", "last_poll_ok_at": "2026-10-06T03:00:00+0000"}
        with mock.patch("tgbridge_core.health.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(polling_health(health)["error"], "service_not_running")
        with mock.patch("tgbridge_core.health.os.kill") as kill:
            for pid in (None, False, "123", 0, -1):
                self.assertFalse(polling_health({"pid": pid})["ok"])
            kill.assert_not_called()

    def test_stale_future_missing_and_naive_poll_timestamps_are_not_healthy(self):
        with mock.patch("tgbridge_core.health.os.kill"), \
             mock.patch("tgbridge_core.health.time.time", return_value=1791255600):
            for stamp, error in (
                ("2026-10-06T02:56:59+0000", "stale_poll"),
                ("2026-10-06T03:01:00+0000", "stale_poll"),
                (None, "invalid_poll_timestamp"),
                ("bad", "invalid_poll_timestamp"),
                ("2026-10-06T03:00:00", "invalid_poll_timestamp"),
            ):
                with self.subTest(stamp=stamp):
                    result = polling_health({"pid": 123, "status": "healthy", "last_poll_ok_at": stamp})
                    self.assertEqual(result["error"], error)

    def test_starting_failed_and_stopped_statuses_are_not_healthy(self):
        with mock.patch("tgbridge_core.health.os.kill"):
            for status in ("starting", "polling", "unhealthy", "crashed", "failed", "stopped"):
                with self.subTest(status=status):
                    self.assertEqual(polling_health({"pid": 123, "status": status})["error"], "poll_not_healthy")

    def test_proxy_credentials_and_paths_are_redacted(self):
        self.assertEqual(
            redact_proxy_url("http://user:secret@127.0.0.1:7897/private"),
            "http://127.0.0.1:7897",
        )

    @mock.patch("tgbridge_core.health.urllib.request.proxy_bypass", return_value=False)
    @mock.patch(
        "tgbridge_core.health.urllib.request.getproxies",
        return_value={"https": "http://user:secret@127.0.0.1:7897"},
    )
    @mock.patch(
        "tgbridge_core.health.socket.create_connection",
        side_effect=ConnectionRefusedError(61, "refused"),
    )
    def test_dead_local_proxy_is_observable(self, _connect, _proxies, _bypass):
        info = proxy_diagnostics()
        self.assertEqual(info["proxies"]["https"], "http://127.0.0.1:7897")
        self.assertEqual(
            info["local_endpoints"],
            [
                {
                    "host": "127.0.0.1",
                    "port": 7897,
                    "listening": False,
                    "source": "https",
                }
            ],
        )
        error = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        self.assertEqual(classify_network_error(error, info), "proxy_refused")

    def test_notify_is_optional_and_nonblocking(self):
        import os
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': ''}):
            self.assertFalse(systemd_notify('WATCHDOG=1'))
        sender = mock.MagicMock()
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': '@fixture', 'WATCHDOG_PID': str(os.getpid())}), \
             mock.patch('tgbridge_core.health.socket.socket') as factory:
            factory.return_value.__enter__.return_value = sender
            self.assertTrue(systemd_notify('WATCHDOG=1'))
        sender.setblocking.assert_called_once_with(False)
        sender.connect.assert_called_once_with('\0fixture')
        sender.send.assert_called_once_with(b'WATCHDOG=1')

    def test_notify_failure_and_child_pid_do_not_break_bridge(self):
        import os
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': '/missing', 'WATCHDOG_PID': str(os.getpid())}), \
             mock.patch('tgbridge_core.health.socket.socket', side_effect=OSError):
            self.assertFalse(systemd_notify('WATCHDOG=1'))
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': '/fixture', 'WATCHDOG_PID': '-1'}), \
             mock.patch('tgbridge_core.health.socket.socket') as factory:
            self.assertFalse(systemd_notify('WATCHDOG=1'))
            factory.assert_not_called()

    def test_bypassed_dead_proxy_is_not_blamed(self):
        info = {
            "telegram_bypassed": True,
            "local_endpoints": [{"listening": False}],
        }
        error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        self.assertEqual(classify_network_error(error, info), "connection_refused")


if __name__ == "__main__":
    unittest.main()
