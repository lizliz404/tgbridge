import urllib.error
import unittest
from unittest import mock

from tgbridge_core.health import (
    classify_network_error,
    proxy_diagnostics,
    redact_proxy_url,
)


class HealthTests(unittest.TestCase):
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

    def test_bypassed_dead_proxy_is_not_blamed(self):
        info = {
            "telegram_bypassed": True,
            "local_endpoints": [{"listening": False}],
        }
        error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        self.assertEqual(classify_network_error(error, info), "connection_refused")


if __name__ == "__main__":
    unittest.main()
